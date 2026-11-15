"""MQTT output with Solar Assistant-compatible topics and Home Assistant discovery.

Topic layout (prefix defaults to ``solar_assistant`` so existing HA / Node-RED
flows keep working after switching from Solar Assistant):

    <prefix>/inverter_1/<metric>/state
    <prefix>/battery_<n>/<metric>/state
    <prefix>/total/<metric>/state
    <prefix>/chargeverter_1/<metric>/state
    <prefix>/inverter_1/<setting>/set      (subscribe; writes when allowed)
"""

from __future__ import annotations

import asyncio
import json
import logging
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
    "generator_power": "generator_power",
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

UNITS = {
    "power": "W", "voltage": "V", "current": "A", "frequency": "Hz", "temperature": "°C",
    "energy": "kWh", "soc": "%", "state_of_charge": "%", "capacity": "Ah", "charge_capacity": "Ah",
}


def _unit_and_class(topic_metric: str) -> tuple[str | None, str | None, str | None]:
    m = topic_metric
    if "energy" in m:
        return "kWh", "energy", "total_increasing"
    if "power" in m:
        return "W", "power", "measurement"
    if "voltage" in m:
        return "V", "voltage", "measurement"
    if "current" in m:
        return "A", "current", "measurement"
    if "frequency" in m:
        return "Hz", "frequency", "measurement"
    if "temperature" in m:
        return "°C", "temperature", "measurement"
    if "state_of_charge" in m or m.endswith("soc"):
        return "%", "battery", "measurement"
    if "capacity" in m:
        return "Ah", None, "measurement"
    if "cycles" in m:
        return None, None, "total_increasing"
    return None, None, None


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

    # -- connection ------------------------------------------------------------
    def _connect(self) -> None:
        import paho.mqtt.client as mqtt

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.cfg.client_id, clean_session=True)
        if self.cfg.username:
            c.username_pw_set(self.cfg.username, self.cfg.password)
        c.will_set(f"{self.cfg.prefix}/solardash/status", "offline", retain=True)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.connect_async(self.cfg.host, self.cfg.port, keepalive=60)
        c.loop_start()
        self._client = c

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self.connected = True
        log.info("mqtt: connected to %s:%d", self.cfg.host, self.cfg.port)
        client.publish(f"{self.cfg.prefix}/solardash/status", "online", retain=True)
        client.subscribe(f"{self.cfg.prefix}/inverter_1/+/set")
        client.subscribe(f"{self.cfg.prefix}/occupancy/set")
        for topic in self.signal_topics:
            client.subscribe(topic)
            log.info("mqtt: listening for generator signal on %s", topic)
        self._discovered.clear()
        self._last.clear()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self.connected = False
        log.warning("mqtt: disconnected (%s)", reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        if msg.topic in self.signal_topics and self.on_signal is not None:
            name, kind = self.signal_topics[msg.topic]
            val = parse_bool_payload(msg.payload.decode(errors="replace"))
            if val is None:
                log.warning("mqtt: unparseable payload %r on %s", msg.payload, msg.topic)
                return
            try:
                self.on_signal(name, val if kind == "running" else None, val if kind == "contactor" else None)
            except Exception as e:  # noqa: BLE001
                log.warning("mqtt: signal handling failed: %s", e)
            return
        parts = msg.topic.split("/")
        if msg.topic == f"{self.cfg.prefix}/occupancy/set" and self.on_occupancy is not None:
            try:
                self.on_occupancy(msg.payload.decode(errors="replace").strip().strip('"').lower())
            except Exception as e:  # noqa: BLE001
                log.warning("mqtt: occupancy set failed: %s", e)
            return
        if len(parts) != 4 or parts[-1] != "set":
            return
        key = parts[2]
        payload = msg.payload.decode(errors="replace").strip()
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
    def _pub(self, topic: str, value: Any, retain: bool = False) -> None:
        if self._client is None or not self.connected:
            return
        if isinstance(value, bool):
            s = "true" if value else "false"
        elif isinstance(value, float):
            s = f"{value:.3f}".rstrip("0").rstrip(".") if value != int(value) else str(int(value))
        elif isinstance(value, (list, dict)):
            s = json.dumps(value)
        else:
            s = str(value)
        if self._last.get(topic) == s:
            return
        self._last[topic] = s
        self._client.publish(topic, s, retain=retain)

    def _discover(self, device_id: str, device_name: str, metric: str, state_topic: str, settable: bool = False) -> None:
        if not self.cfg.ha_discovery or state_topic in self._discovered or self._client is None:
            return
        self._discovered.add(state_topic)
        unit, dev_class, state_class = _unit_and_class(metric)
        uid = f"solardash_{device_id}_{metric}".replace("-", "_").replace("/", "_")
        payload: dict[str, Any] = {
            "name": metric.replace("_", " ").replace("-", "").strip().capitalize(),
            "unique_id": uid,
            "state_topic": state_topic,
            "availability_topic": f"{self.cfg.prefix}/solardash/status",
            "device": {"identifiers": [f"solardash_{device_id}"], "name": device_name, "manufacturer": "solardash"},
        }
        if unit:
            payload["unit_of_measurement"] = unit
        if dev_class:
            payload["device_class"] = dev_class
        if state_class:
            payload["state_class"] = state_class
        self._client.publish(f"{self.cfg.ha_prefix}/sensor/{uid}/config", json.dumps(payload), retain=True)

    def publish_snapshot(self) -> None:
        p = self.cfg.prefix
        inv = self.state.inverter
        if inv:
            seen = set()
            for key, val in inv.items():
                if key.startswith("_") or isinstance(val, (dict, list)):
                    continue
                name = SA_INVERTER.get(key, key)
                if name in seen:
                    continue
                seen.add(name)
                topic = f"{p}/inverter_1/{name}/state"
                self._discover("inverter_1", "Inverter", name, topic)
                self._pub(topic, val)
            for key, val in self.state.inverter_settings.items():
                if isinstance(val, (dict, list)):
                    continue
                topic = f"{p}/inverter_1/{key}/state"
                self._pub(topic, val)
        for idx, b in sorted(self.state.batteries.items()):
            for key, val in b.items():
                if key.startswith("_") or isinstance(val, (dict, list)) or val is None:
                    continue
                name = SA_BATTERY.get(key, key)
                topic = f"{p}/battery_{idx}/{name}/state"
                self._discover(f"battery_{idx}", b.get("name", f"Battery {idx}"), name, topic)
                self._pub(topic, val)
            for k, t in enumerate(b.get("temperatures", []), 1):
                self._pub(f"{p}/battery_{idx}/temperature_{k}/state", t)
        for key, val in self.state.derived.items():
            if key.startswith("_") or isinstance(val, (dict, list)) or val is None:
                continue
            topic = f"{p}/total/{key}/state"
            self._discover("total", "Totals", key, topic)
            self._pub(topic, val)
        for i, g in enumerate(self.state.derived.get("generators", []), 1):
            for key in ("name", "active", "running", "source", "contactor", "warming_up", "delivering", "runtime_today_h"):
                if g.get(key) is not None:
                    topic = f"{p}/generator_{i}/{key}/state"
                    if key == "runtime_today_h":
                        self._discover(f"generator_{i}", g["name"], "runtime_today_h", topic)
                    self._pub(topic, g[key])
        w = (self.state.weather or {}).get("current") or {}
        for key, val in w.items():
            if isinstance(val, (int, float)) and not isinstance(val, bool) and key not in ("ts",):
                topic = f"{p}/weather/{key}/state"
                self._discover("weather", "Weather station", key, topic)
                self._pub(topic, val)
        if self.extra_values is not None:
            try:
                for key, val in self.extra_values().items():
                    topic = f"{p}/{key}/state"
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        self._discover(key.split("/")[0], key.split("/")[0].capitalize(), key.split("/")[1], topic)
                    self._pub(topic, val)
            except Exception as e:  # noqa: BLE001
                log.debug("mqtt: extra values failed: %s", e)
        cvd = self.state.chargeverter
        if cvd:
            for key in ("charger_online", "battery_answering", "poll_count", "mode", "presented_soc", "answered"):
                if key in cvd and cvd[key] is not None:
                    self._pub(f"{p}/chargeverter_1/{key}/state", cvd[key])
            lr = cvd.get("last_reply") or {}
            for key, val in lr.items():
                if val is not None:
                    self._pub(f"{p}/chargeverter_1/reported_{key}/state", val)

    async def run(self) -> None:
        try:
            self._connect()
        except Exception as e:  # noqa: BLE001
            log.error("mqtt: cannot start client: %s", e)
            return
        while not self._stop.is_set():
            try:
                self.publish_snapshot()
            except Exception as e:  # noqa: BLE001
                log.warning("mqtt: publish failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.publish_interval)
            except asyncio.TimeoutError:
                pass
        if self._client is not None:
            try:
                self._client.publish(f"{self.cfg.prefix}/solardash/status", "offline", retain=True)
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self._stop.set()
