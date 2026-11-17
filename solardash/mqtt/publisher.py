"""MQTT output with Solar Assistant-compatible topics and Home Assistant discovery.

Topic layout (prefix defaults to ``solar_assistant`` so existing HA / Node-RED
flows keep working after switching from Solar Assistant):

    <prefix>/inverter_1/<metric>/state       live data and settings
    <prefix>/battery_<n>/<metric>/state
    <prefix>/total/<metric>/state            bank totals, generator state, occupancy
    <prefix>/generator_<n>/<metric>/state
    <prefix>/weather/<metric>/state
    <prefix>/forecast/<metric>/state
    <prefix>/plan/<metric>/state             generator plan (recommended_now, start_at, ...)
    <prefix>/chargeverter_1/<metric>/state   only with the optional RS485 link
    <prefix>/solardash/status                online/offline (availability)

Subscriptions:

    <prefix>/inverter_1/<setting>/set        change an inverter setting (needs allow_writes)
    <prefix>/occupancy/set                   occupied | away
    <generator running_topic / contactor_topic from config>

Every scalar value is announced to Home Assistant through MQTT discovery:
booleans as ``binary_sensor``, numbers as ``sensor`` with unit/device class
guessed from the metric name, strings as plain ``sensor``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from ..config import MqttConfig
from ..core.state import SystemState

log = logging.getLogger(__name__)

# our inverter key -> Solar Assistant topic name
SA_INVERTER = {
    "pv_power": "pv_power",
    "pv1_power": "pv_power_1",
    "pv2_power": "pv_power_2",
    "pv1_voltage": "pv_voltage_1",
    "pv2_voltage": "pv_voltage_2",
    "battery_power": "battery_power",
    "battery_voltage": "battery_voltage",
    "battery_soc": "battery_state_of_charge",
    "battery_charge_power": "battery_charge_power",
    "battery_discharge_power": "battery_discharge_power",
    "load_power": "load_power",
    "eps_l1_power": "load_power_1",
    "eps_l2_power": "load_power_2",
    "eps_power": "ac_output_power",
    "eps_voltage": "ac_output_voltage",
    "eps_frequency": "ac_output_frequency",
    "eps_l1_voltage": "ac_output_voltage_1",
    "eps_l2_voltage": "ac_output_voltage_2",
    "temperature_inner": "temperature",
    "temperature_radiator_1": "temperature_dc",
    "temperature_radiator_2": "temperature_ac",
    "temperature_battery": "battery_temperature",
    "status": "device_mode",
    "fault_code": "fault_code",
    "warning_code": "warning_code",
    "pv_energy_today": "pv_energy_today",
    "load_energy_today": "load_energy_today",
    "bus_voltage_1": "bus_voltage_1",
}
SA_BATTERY = {
    "voltage": "voltage",
    "current": "current",
    "power": "power",
    "soc": "state_of_charge",
    "temperature": "temperature",
    "cycles": "cycles",
    "nominal_ah": "capacity",
    "remaining_ah": "charge_capacity",
    "cell_max": "cell_voltage_-_highest",
    "cell_min": "cell_voltage_-_lowest",
    "cell_avg": "cell_voltage_-_average",
    "cell_imbalance": "cell_voltage_-_imbalance",
}

# metrics that are diagnostics in HA (hidden from the main device page)
DIAGNOSTIC_HINTS = ("code", "bus_voltage", "_raw", "status_code", "poll_count", "answered", "samples", "source", "age")
BINARY_CLASS = {"running": "running", "online": "connectivity", "answering": "connectivity", "connected": "connectivity",
                "recommended_now": None, "stop_recommended": None, "needed": None, "warming_up": None,
                "contactor": "power", "ac_input_live": "power", "active": None, "delivering": "power", "live": "power",
                "charge_fet_on": "power", "discharge_fet_on": "power"}


def _unit_and_class(metric: str) -> tuple[str | None, str | None, str | None]:
    m = metric
    if m.endswith("_h") or "runtime" in m or m.endswith("run_h"):
        return "h", "duration", "total_increasing" if "runtime" in m else None
    if "energy" in m or m.endswith("_kwh"):
        return "kWh", "energy", "total_increasing" if "today" in m or "total" in m else "measurement"
    if m.endswith("_kw"):
        return "kW", "power", "measurement"
    if "power" in m:
        return "W", "power", "measurement"
    if "voltage" in m:
        return "V", "voltage", "measurement"
    if "current" in m and "according" not in m:
        return "A", "current", "measurement"
    if "frequency" in m:
        return "Hz", "frequency", "measurement"
    if "temperature" in m or "temp_" in m:
        return "°C", "temperature", "measurement"
    if "humidity" in m:
        return "%", "humidity", "measurement"
    if "pressure" in m:
        return "hPa", "atmospheric_pressure", "measurement"
    if "radiation" in m:
        return "W/m²", "irradiance", "measurement"
    if "wind" in m and "kph" in m:
        return "km/h", "wind_speed", "measurement"
    if "precip" in m or "rain" in m:
        return "mm", None, "measurement"
    if "state_of_charge" in m or m.endswith("soc") or "soc_" in m:
        return "%", "battery", "measurement"
    if "capacity" in m and "ah" in m or m in ("capacity", "charge_capacity"):
        return "Ah", None, "measurement"
    if "percent" in m or "share" in m:
        return "%", None, "measurement"
    if "cycles" in m:
        return None, None, "total_increasing"
    return None, None, "measurement"


TRUE_WORDS = {"on", "true", "1", "yes", "running", "closed", "open_contact_closed"}
FALSE_WORDS = {"off", "false", "0", "no", "stopped", "open", "unavailable", "unknown"}


def parse_bool_payload(payload: str) -> bool | None:
    p = payload.strip().strip('"').lower()
    if p in TRUE_WORDS:
        return True
    if p in FALSE_WORDS:
        return False
    try:
        return float(p) != 0.0
    except ValueError:
        return None


def format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".") if value != int(value) else str(int(value))
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


class MqttPublisher:
    def __init__(
        self,
        cfg: MqttConfig,
        state: SystemState,
        write_setting: Callable[[str, Any], Any] | None = None,
        signal_topics: dict[str, tuple[str, str]] | None = None,
        on_signal: Callable[[str, bool | None, bool | None], Any] | None = None,
        extra_values: Callable[[], dict[str, Any]] | None = None,
        on_occupancy: Callable[[str], Any] | None = None,
    ) -> None:
        self.extra_values = extra_values
        self.on_occupancy = on_occupancy
        self.cfg = cfg
        self.state = state
        self.write_setting = write_setting
        # topic -> (generator name, "running"|"contactor")
        self.signal_topics = signal_topics or {}
        self.on_signal = on_signal
        self._client = None
        self._discovered: set[str] = set()
        self._last: dict[str, str] = {}
        self._stop = asyncio.Event()
        self.connected = False
        self.stats = {"published": 0, "discovered": 0, "received": 0}
        self.last_error: str | None = None

    # -- connection ------------------------------------------------------------
    def _connect(self) -> None:
        import paho.mqtt.client as mqtt

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.cfg.client_id, clean_session=True)
        if self.cfg.username:
            c.username_pw_set(self.cfg.username, self.cfg.password)
        c.will_set(self.availability_topic, "offline", retain=True)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.on_connect_fail = self._on_connect_fail
        self._connect_started = time.monotonic()
        c.reconnect_delay_set(min_delay=1, max_delay=60)
        c.connect_async(self.cfg.host, self.cfg.port, keepalive=60)
        c.loop_start()
        self._client = c

    @property
    def availability_topic(self) -> str:
        return f"{self.cfg.prefix}/solardash/status"

    def _explain(self, reason) -> str:
        r = str(reason).lower()
        where = f"{self.cfg.host}:{self.cfg.port}"
        if self.cfg.host.strip().lower() in ("localhost", "127.0.0.1", "::1", ""):
            return "Broker address must be Home Assistant's IP address (e.g. 192.168.1.50); 'localhost' means the solardash container itself."
        if "not authorized" in r or "bad user" in r or "password" in r:
            return f"Broker at {where} rejected the username or password."
        if "refused" in r and "connection" in r:
            return f"Nothing is listening at {where}. Check the address and that the Mosquitto add-on is running."
        if "name or service" in r or "nodename" in r or "getaddrinfo" in r:
            return f"Can't find a host named {self.cfg.host}. Use Home Assistant's IP address."
        if "timed out" in r or "unreachable" in r:
            return f"No answer from {where}. Check the address and that the NAS can reach it."
        return f"Broker at {where}: {reason}"

    def _on_connect_fail(self, client, userdata) -> None:
        self.connected = False
        self.last_error = self.last_error or f"No answer from {self.cfg.host}:{self.cfg.port}. Check the address and port."
        log.warning("mqtt: %s", self.last_error)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            self.connected = False
            self.last_error = self._explain(reason_code)
            log.warning("mqtt: %s", self.last_error)
            return
        self.connected = True
        self.last_error = None
        log.info("mqtt: connected to %s:%d", self.cfg.host, self.cfg.port)
        client.publish(self.availability_topic, "online", retain=True)
        client.subscribe(f"{self.cfg.prefix}/inverter_1/+/set")
        client.subscribe(f"{self.cfg.prefix}/occupancy/set")
        client.subscribe(f"{self.cfg.ha_prefix}/status")  # Home Assistant birth message
        for topic in self.signal_topics:
            client.subscribe(topic)
            log.info("mqtt: listening for generator signal on %s", topic)
        # re-announce everything after a (re)connect
        self._discovered.clear()
        self._last.clear()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self.connected = False
        log.warning("mqtt: disconnected (%s); will reconnect", reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        self.stats["received"] += 1
        payload = msg.payload.decode(errors="replace").strip()
        if msg.topic == f"{self.cfg.ha_prefix}/status":
            if payload.lower() == "online":
                log.info("mqtt: Home Assistant came online; re-announcing everything")
                self._discovered.clear()
                self._last.clear()
            return
        if msg.topic in self.signal_topics and self.on_signal is not None:
            name, kind = self.signal_topics[msg.topic]
            val = parse_bool_payload(payload)
            if val is None:
                log.warning("mqtt: unparseable payload %r on %s", payload, msg.topic)
                return
            try:
                self.on_signal(name, val if kind == "running" else None, val if kind == "contactor" else None)
            except Exception as e:  # noqa: BLE001
                log.warning("mqtt: signal handling failed: %s", e)
            return
        if msg.topic == f"{self.cfg.prefix}/occupancy/set" and self.on_occupancy is not None:
            try:
                self.on_occupancy(payload.strip('"').lower())
            except Exception as e:  # noqa: BLE001
                log.warning("mqtt: occupancy set failed: %s", e)
            return
        parts = msg.topic.split("/")
        if len(parts) != 4 or parts[-1] != "set":
            return
        key = parts[2]
        if self.write_setting is None:
            log.warning("mqtt: write requested for %s but writes are not wired", key)
            return
        try:
            value: Any = payload
            if payload.lower() in ("on", "true", "enabled"):
                value = True
            elif payload.lower() in ("off", "false", "disabled"):
                value = False
            else:
                try:
                    value = float(payload) if "." in payload else int(payload)
                except ValueError:
                    pass
            self.write_setting(key, value)
        except Exception as e:  # noqa: BLE001
            log.warning("mqtt: set %s=%r failed: %s", key, payload, e)

    # -- publishing --------------------------------------------------------------
    def _raw_publish(self, topic: str, payload: str, retain: bool = False) -> None:
        if self._client is None:
            return
        self._client.publish(topic, payload, retain=retain)
        self.stats["published"] += 1

    def _pub(self, topic: str, value: Any, retain: bool = True) -> None:
        if self._client is None or not self.connected:
            return
        s = format_value(value)
        if self._last.get(topic) == s:
            return
        self._last[topic] = s
        self._raw_publish(topic, s, retain=retain)

    def _discover(self, device_id: str, device_name: str, metric: str, state_topic: str, value: Any,
                  diagnostic: bool = False) -> None:
        if not self.cfg.ha_discovery or state_topic in self._discovered or self._client is None:
            return
        if isinstance(value, (list, dict)) or value is None:
            return
        self._discovered.add(state_topic)
        uid = f"solardash_{device_id}_{metric}".replace("-", "_").replace("/", "_").replace(" ", "_")
        pretty = metric.replace("_-_", " ").replace("_", " ").strip().capitalize()
        payload: dict[str, Any] = {
            "name": pretty,
            "unique_id": uid,
            "object_id": uid,
            "state_topic": state_topic,
            "availability_topic": self.availability_topic,
            "device": {"identifiers": [f"solardash_{device_id}"], "name": device_name, "manufacturer": "solardash",
                       "model": "solardash", "sw_version": "0.1"},
            "origin": {"name": "solardash"},
        }
        if isinstance(value, bool):
            component = "binary_sensor"
            payload["payload_on"] = "true"
            payload["payload_off"] = "false"
            dev_class = next((cls for key, cls in BINARY_CLASS.items() if key in metric), None)
            if dev_class:
                payload["device_class"] = dev_class
        else:
            component = "sensor"
            if isinstance(value, (int, float)):
                unit, dev_class, state_class = _unit_and_class(metric)
                if unit:
                    payload["unit_of_measurement"] = unit
                if dev_class:
                    payload["device_class"] = dev_class
                if state_class:
                    payload["state_class"] = state_class
            elif isinstance(value, str) and metric.endswith("_at"):
                payload["device_class"] = "timestamp"
        if diagnostic or any(h in metric for h in DIAGNOSTIC_HINTS):
            payload["entity_category"] = "diagnostic"
        self._raw_publish(f"{self.cfg.ha_prefix}/{component}/{uid}/config", json.dumps(payload), retain=True)
        self.stats["discovered"] += 1

    def _emit(self, device_id: str, device_name: str, metric: str, value: Any, diagnostic: bool = False) -> None:
        if value is None:
            return
        topic = f"{self.cfg.prefix}/{device_id}/{metric}/state"
        if topic not in self._discovered:
            self._discover(device_id, device_name, metric, topic, value, diagnostic=diagnostic)
            self._new_since_flush = True
        self._pub(topic, value)

    def publish_snapshot(self) -> None:
        st = self.state
        inv = st.inverter
        if inv:
            seen = set()
            for key, val in inv.items():
                if key.startswith("_") or isinstance(val, dict):
                    continue
                name = SA_INVERTER.get(key, key)
                if name in seen:
                    continue
                seen.add(name)
                self._emit("inverter_1", "Inverter", name, val, diagnostic=isinstance(val, list))
            for key, val in st.inverter_settings.items():
                if isinstance(val, (dict, list)):
                    continue
                self._emit("inverter_1", "Inverter", key, val, diagnostic=True)
        for idx, b in sorted(st.batteries.items()):
            dev = f"battery_{idx}"
            dname = b.get("name", f"Battery {idx}")
            for key, val in b.items():
                if key.startswith("_") or isinstance(val, dict) or val is None:
                    continue
                if isinstance(val, list):
                    if key == "protection":
                        self._emit(dev, dname, "protection", ", ".join(val) or "none")
                    continue
                self._emit(dev, dname, SA_BATTERY.get(key, key), val)
            for k, t in enumerate(b.get("temperatures", []), 1):
                self._emit(dev, dname, f"temperature_{k}", t)
        for key, val in st.derived.items():
            if key.startswith("_") or isinstance(val, dict) or val is None:
                continue
            if isinstance(val, list):
                if key in ("battery_protection_active", "generator_warnings"):
                    self._emit("total", "Totals", key, "; ".join(str(x) for x in val) or "none")
                continue
            self._emit("total", "Totals", key, val)
        for i, g in enumerate(st.derived.get("generators", []), 1):
            for key in ("name", "active", "running", "source", "contactor", "warming_up", "delivering", "runtime_today_h"):
                if g.get(key) is not None:
                    self._emit(f"generator_{i}", g["name"], key, g[key])
        w = (st.weather or {}).get("current") or {}
        for key, val in w.items():
            if key in ("ts", "raw") or val is None or isinstance(val, (dict, list)):
                continue
            self._emit("weather", "Weather station", key, val, diagnostic=key in ("station_id", "lat", "lon", "age_s"))
        if self.extra_values is not None:
            try:
                for key, val in self.extra_values().items():
                    dev, metric = key.split("/", 1)
                    self._emit(dev, dev.capitalize(), metric, val)
            except Exception as e:  # noqa: BLE001
                log.debug("mqtt: extra values failed: %s", e)
        cvd = st.chargeverter
        if cvd:
            for key in ("charger_online", "battery_answering", "poll_count", "mode", "presented_soc", "answered"):
                if cvd.get(key) is not None:
                    self._emit("chargeverter_1", "Chargeverter", key, cvd[key])
            for key, val in (cvd.get("last_reply") or {}).items():
                self._emit("chargeverter_1", "Chargeverter", f"reported_{key}", val)

    def _probe(self) -> str:
        import socket

        try:
            with socket.create_connection((self.cfg.host, int(self.cfg.port)), timeout=4):
                return f"Reached {self.cfg.host}:{self.cfg.port} but the broker did not accept the session. Check username and password."
        except OSError as e:
            return self._explain(e)

    def published_topics(self) -> list[str]:
        return sorted(self._last)

    async def wait_connected(self, timeout: float = 10.0) -> bool:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self.connected:
                return True
            await asyncio.sleep(0.1)
        return False

    async def run(self) -> None:
        try:
            self._connect()
        except Exception as e:  # noqa: BLE001
            log.error("mqtt: cannot start client: %s", e)
            self.last_error = str(e)
            return
        try:
            await self._loop()
        finally:
            self.close()  # also on cancel: never leave a second client with the same id running

    async def _loop(self) -> None:
        while not self._stop.is_set():
            if not self.connected and self.cfg.host.strip().lower() in ("localhost", "127.0.0.1", "::1", ""):
                self.last_error = self._explain("")
            elif not self.connected and not self.last_error and time.monotonic() - self._connect_started > 12:
                # paho retries silently; probe once so the UI can say why
                self.last_error = self._probe()
            if time.monotonic() - getattr(self, "_full_at", 0) > 300:
                self._last.clear()  # every 5 min re-send unchanged values too
                self._full_at = time.monotonic()
            try:
                if self.connected:
                    self.publish_snapshot()
            except Exception as e:  # noqa: BLE001
                log.warning("mqtt: publish failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.publish_interval)
            except asyncio.TimeoutError:
                pass

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.publish(self.availability_topic, "offline", retain=True)
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def stop(self) -> None:
        self._stop.set()
