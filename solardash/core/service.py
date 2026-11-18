"""Wires config -> devices -> state -> history/MQTT/API."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

from ..config import Config
from ..devices.battery import BatteryPoller
from ..devices.chargeverter import ChargeverterTask
from ..devices.inverter import InverterPoller
from ..forecast.service import ForecastTask, WeatherTask, plan_mqtt_values
from ..mqtt.publisher import MqttPublisher
from ..storage.sqlite import HistoryStore
from .derived import compute_totals
from .state import SystemState

log = logging.getLogger(__name__)

# Which inverter keys go to history (bounded DB growth).
INVERTER_HISTORY_KEYS = [
    "pv_power", "pv1_power", "pv2_power", "pv1_voltage", "pv2_voltage", "battery_power", "battery_voltage",
    "battery_soc", "load_power", "eps_power", "eps_l1_power", "eps_l2_power", "eps_voltage", "eps_frequency",
    "ac_input_voltage", "ac_input_frequency", "ac_input_power", "ac_input_total_power", "rectifier_power", "temperature_inner",
    "temperature_radiator_1", "temperature_radiator_2", "temperature_battery", "bms_battery_current",
    "bus_voltage_1", "status_code", "fault_code", "warning_code",
]
TOTAL_HISTORY_KEYS = [
    "battery_power", "battery_voltage", "battery_current", "battery_state_of_charge", "battery_temperature",
    "battery_cell_voltage_min", "battery_cell_voltage_max", "battery_cell_imbalance_max", "chargeverter_power",
    "chargeverter_power_raw", "pv_power", "load_power", "generator_power",
]
BATTERY_HISTORY_KEYS = ["voltage", "current", "power", "soc", "temperature", "cell_min", "cell_max", "cell_imbalance"]
DAILY_ENERGY_KEYS = [
    "pv_energy_today", "load_energy_today", "battery_charge_energy_today", "battery_discharge_energy_today",
    "rectifier_energy_today", "ac_input_energy_today", "ac_input_total_energy_today", "eps_energy_today",
]
ACTIVE_GENERATOR_KEY = "active_generator"
OCCUPANCY_KEY = "occupancy_mode"
GEN_ON_DELAY_S = 20.0   # inferred output must persist this long to count as "running"
GEN_OFF_DELAY_S = 45.0  # and be gone this long to count as "stopped"


def runtime_metric(name: str) -> str:
    return f"generator_runtime_h/{name}"


class Service:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.state = SystemState()
        self.store = HistoryStore(cfg.history.path, cfg.history.retention_days, int(cfg.history.max_db_gb * 1e9))
        self.inverter: InverterPoller | None = None
        self.batteries: list[BatteryPoller] = []
        self.chargeverter: ChargeverterTask | None = None
        self.weather: WeatherTask | None = None
        self.forecast: ForecastTask | None = None
        self.mqtt: MqttPublisher | None = None
        self._tasks: list[asyncio.Task] = []
        self._named: dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()
        self._runtime_day: str | None = None
        self._today_energy: dict[str, float] = {}
        self._today_energy_at = 0.0
        self._runtime: dict[str, float] = {}  # generator name -> hours today
        self._running_prev: dict[str, bool | None] = {}
        self._run_started: dict[str, float] = {}  # generator name -> when the current run began
        self._wear: dict[str, dict[str, float]] | None = None  # name -> {"fuel": h since refill, "oil": h since oil change}
        self._wear_saved = 0.0
        self._raw_running: bool = False
        self._raw_since: float = 0.0
        self._last_track: float | None = None
        # external signals per generator: {"running": bool|None, "contactor": bool|None, "updated": ts}
        self.signals: dict[str, dict[str, Any]] = {}
        self._build()

    # -- construction ------------------------------------------------------------
    def _build(self) -> None:
        cfg = self.cfg
        world = None
        if cfg.simulate:
            from ..devices.simulated import SimWorld

            world = SimWorld(packs=max(1, len(cfg.batteries)))
            log.warning("SIMULATION MODE: no real hardware is being read")
        self._world = world
        self._build_devices()
        self._build_weather()
        self._build_mqtt()

    def _build_devices(self) -> None:
        cfg = self.cfg
        world = self._world
        if world is not None:
            from ..devices.simulated import SimulatedBatteryLink, SimulatedChargeverterLink, SimulatedInverterLink
        for k in [k for k in self.state.devices if k.startswith(("inverter:", "battery:", "chargeverter:"))]:
            self.state.devices.pop(k, None)
        self.inverter = None
        self.batteries = []
        self.chargeverter = None
        if cfg.inverter.enabled:
            link = SimulatedInverterLink(world, cfg.inverter.unit) if world else None
            self.inverter = InverterPoller(cfg.inverter, self.state, link=link)
        self.state.batteries.clear()
        self.state.battery_updated.clear()
        for i, b in enumerate(cfg.batteries, 1):
            if not b.enabled:
                continue
            link = SimulatedBatteryLink(world, i - 1) if world else None
            self.batteries.append(BatteryPoller(i, b, self.state, link=link))
        if cfg.chargeverter.enabled and cfg.chargeverter.mode in ("sniff", "emulate"):
            link = SimulatedChargeverterLink(world, with_battery=(cfg.chargeverter.mode == "sniff")) if world else None
            self.chargeverter = ChargeverterTask(cfg.chargeverter, self.state, link=link)

    def _build_weather(self) -> None:
        cfg = self.cfg
        world = self._world
        self.weather = None
        self.state.devices.pop("weather:wunderground", None)
        self.state.devices.pop("weather:openmeteo", None)
        if cfg.weather.enabled:
            fetch = None
            if world is not None:
                from ..devices.simulated import SimulatedWeatherFetch

                fetch = SimulatedWeatherFetch(world)
            self.weather = WeatherTask(cfg, self.state, self.store, fetch=fetch)
        om_fetch = None
        if world is not None:
            from ..devices.simulated import SimulatedOpenMeteoFetch

            om_fetch = SimulatedOpenMeteoFetch(world)
        self.forecast = ForecastTask(cfg, self.state, self.store, self.weather, openmeteo_fetch=om_fetch,
                                     occupancy_mode=self.occupancy_mode)

    def _build_mqtt(self) -> None:
        cfg = self.cfg
        self.mqtt = None
        self.state.devices.pop("mqtt:broker", None)
        if cfg.mqtt.enabled:
            topics: dict[str, tuple[str, str]] = {}
            for g in cfg.generators:
                if g.running_topic:
                    topics[g.running_topic] = (g.name, "running")
                if g.contactor_topic:
                    topics[g.contactor_topic] = (g.name, "contactor")
            self.mqtt = MqttPublisher(cfg.mqtt, self.state, write_setting=self.write_setting_blocking,
                                      signal_topics=topics, on_signal=self.set_generator_signal,
                                      extra_values=lambda: plan_mqtt_values(self.state.forecast),
                                      on_occupancy=self.set_occupancy, on_service=self.generator_service,
                                      on_panels_cleared=lambda: self.forecast.panels_cleared() if self.forecast else None)
            self.mqtt._occupancy_modes = cfg.occupancy.modes if cfg.occupancy.enabled else []
            from .state import DeviceHealth

            self._mqtt_health = DeviceHealth(name="MQTT broker", kind="mqtt", port=f"{cfg.mqtt.host}:{cfg.mqtt.port}")
            self.state.devices["mqtt:broker"] = self._mqtt_health

    # -- lifecycle -----------------------------------------------------------------
    async def start(self) -> None:
        self._start_device_tasks()
        self._start_weather_tasks()
        self._start_mqtt_task()
        self._tasks.append(asyncio.create_task(self._derive_loop(), name="derive"))
        self._tasks.append(asyncio.create_task(self._sample_loop(), name="sample"))
        self.store.add_event("info", "service", "solardash started" + (" (simulation)" if self.cfg.simulate else ""))

    def _start_device_tasks(self) -> None:
        if self.inverter:
            self._named["inverter"] = asyncio.create_task(self.inverter.run(), name="inverter")
        for b in self.batteries:
            self._named[f"battery{b.index}"] = asyncio.create_task(b.run(), name=f"battery{b.index}")
        if self.chargeverter:
            self._named["chargeverter"] = asyncio.create_task(self.chargeverter.run(), name="chargeverter")

    async def _stop_device_tasks(self) -> None:
        names = [n for n in self._named if n == "inverter" or n.startswith("battery") or n == "chargeverter"]
        for p in ([self.inverter] if self.inverter else []) + list(self.batteries) + ([self.chargeverter] if self.chargeverter else []):
            p.stop()
        for n in names:
            t = self._named.pop(n)
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for p in ([self.inverter] if self.inverter else []) + list(self.batteries):
            try:
                p.link.close()
            except Exception:  # noqa: BLE001
                pass

    def _start_weather_tasks(self) -> None:
        if self.weather:
            self._named["weather"] = asyncio.create_task(self.weather.run(), name="weather")
        if self.forecast:
            self._named["forecast"] = asyncio.create_task(self.forecast.run(), name="forecast")

    def _start_mqtt_task(self) -> None:
        if self.mqtt:
            self._named["mqtt"] = asyncio.create_task(self.mqtt.run(), name="mqtt")

    async def _stop_named(self, *names: str) -> None:
        for n in names:
            t = self._named.pop(n, None)
            if t is None:
                continue
            obj = {"weather": self.weather, "forecast": self.forecast, "mqtt": self.mqtt}.get(n)
            if obj is not None:
                obj.stop()
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def reconfigure(self, section: str) -> str:
        """Apply a changed config section live. Returns what was restarted."""
        if section in ("inverter", "batteries"):
            await self._stop_device_tasks()
            self._build_devices()
            self._start_device_tasks()
            return "serial pollers restarted"
        if section in ("weather", "planner", "occupancy"):
            await self._stop_named("weather", "forecast")
            self._build_weather()
            self._start_weather_tasks()
            return "weather and forecast restarted"
        if section in ("mqtt", "generators"):
            await self._stop_named("mqtt")
            self._build_mqtt()
            self._start_mqtt_task()
            self._running_prev = {}
            return "mqtt restarted"
        return "no restart needed"

    async def stop(self) -> None:
        self._stop.set()
        if self.inverter:
            self.inverter.stop()
        for b in self.batteries:
            b.stop()
        if self.chargeverter:
            self.chargeverter.stop()
        if self.weather:
            self.weather.stop()
        if self.forecast:
            self.forecast.stop()
        if self.mqtt:
            self.mqtt.stop()
        for t in list(self._tasks) + list(self._named.values()):
            t.cancel()
        await asyncio.gather(*self._tasks, *self._named.values(), return_exceptions=True)
        self.store.close()

    # -- loops ---------------------------------------------------------------------
    async def _derive_loop(self) -> None:
        last_health: dict[str, bool] = {}
        while not self._stop.is_set():
            try:
                self.state.derived = compute_totals(
                    self.state.inverter, self.state.batteries, self.state.chargeverter,
                    threshold_w=self.cfg.chargeverter.power_threshold_w,
                )
                self._track_generators()
                await self._apply_today_energy()
                self.state.derived["occupancy"] = self.occupancy_mode()
                if self.mqtt is not None and hasattr(self, "_mqtt_health"):
                    h = self._mqtt_health
                    h.connected = self.mqtt.connected
                    if h.connected:
                        h.last_ok = time.time()
                    h.last_error = self.mqtt.last_error
                    h.poll_count = self.mqtt.stats["published"]
                    h.stats = dict(self.mqtt.stats)
                self.state.bus.publish("total", None)
                # device up/down events
                for k, h in self.state.devices.items():
                    prev = last_health.get(k)
                    if prev is not None and prev != h.connected:
                        self.store.add_event("warn" if not h.connected else "info", k,
                                             "connected" if h.connected else f"lost: {h.last_error}")
                    last_health[k] = h.connected
            except Exception as e:  # noqa: BLE001
                log.exception("derive loop: %s", e)
            await asyncio.sleep(1.0)

    def _integrate_today(self) -> dict[str, float]:
        """kWh since local midnight from the stored power samples (gaps > 10 min skipped)."""
        now = time.time()
        midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        out: dict[str, float] = {}
        for metric, key in (("total/pv_power", "pv_energy_today"), ("total/load_power", "load_energy_today")):
            pts = self.store.query(metric, midnight, now)
            wh = 0.0
            for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
                dt = t1 - t0
                if 0 < dt <= 600:
                    wh += max(0.0, (v0 + v1) / 2) * dt / 3600
            out[key] = round(wh / 1000, 2)
        return out

    async def _apply_today_energy(self) -> None:
        """Use the inverter's daily counters when it fills them; otherwise our own integration."""
        if time.time() - self._today_energy_at > 60:
            self._today_energy_at = time.time()
            try:
                self._today_energy = await asyncio.to_thread(self._integrate_today)
            except Exception as e:  # noqa: BLE001
                log.debug("today energy: %s", e)
        d = self.state.derived
        for key, val in self._today_energy.items():
            reg = d.get(key) or 0
            if val > reg:  # some 6000XP firmware leaves these counters at 0
                d[key] = val

    def _collect_sample(self) -> dict[str, Any]:
        m: dict[str, Any] = {}
        inv = self.state.inverter
        now = time.time()
        if inv and now - inv.get("_updated", 0) < 60:
            for k in INVERTER_HISTORY_KEYS:
                if k in inv:
                    m[f"inverter/{k}"] = inv[k]
        for k in TOTAL_HISTORY_KEYS:
            v = self.state.derived.get(k)
            if v is not None:
                m[f"total/{k}"] = v
        if self.cfg.occupancy.enabled:
            mode = self.occupancy_mode()
            if mode in self.cfg.occupancy.modes:
                m["total/occupancy"] = self.cfg.occupancy.modes.index(mode)
        for idx, b in self.state.batteries.items():
            if now - b.get("_updated", 0) < 60:
                for k in BATTERY_HISTORY_KEYS:
                    if b.get(k) is not None:
                        m[f"battery_{idx}/{k}"] = b[k]
        return m

    async def _sample_loop(self) -> None:
        interval = self.cfg.history.sample_interval
        while not self._stop.is_set():
            try:
                ts = int(time.time())
                sample = self._collect_sample()
                if sample:
                    await asyncio.to_thread(self.store.insert, ts, sample)
                inv = self.state.inverter
                if inv and ts - inv.get("_updated", 0) < 60:
                    day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                    daily = {k: inv[k] for k in DAILY_ENERGY_KEYS if k in inv}
                    if daily:
                        await asyncio.to_thread(self.store.set_daily_energy, day, daily)
                if self._runtime_day == datetime.fromtimestamp(ts).strftime("%Y-%m-%d"):
                    await asyncio.to_thread(self.store.set_daily_energy, self._runtime_day,
                                            {runtime_metric(n): h for n, h in self._runtime.items()})
                await asyncio.to_thread(self.store.maybe_rollup)
            except Exception as e:  # noqa: BLE001
                log.exception("sample loop: %s", e)
            await asyncio.sleep(interval)

    # -- occupancy ---------------------------------------------------------------------
    def occupancy_mode(self) -> str | None:
        if not self.cfg.occupancy.enabled:
            return None
        stored = self.store.get_kv(OCCUPANCY_KEY)
        return stored if stored in self.cfg.occupancy.modes else self.cfg.occupancy.default

    def set_occupancy(self, mode: str) -> str:
        if mode not in self.cfg.occupancy.modes:
            raise KeyError(f"unknown occupancy mode {mode!r}; configured: {self.cfg.occupancy.modes}")
        if self.occupancy_mode() != mode:
            self.store.set_kv(OCCUPANCY_KEY, mode)
            self.store.add_event("info", "occupancy", f"mode set to {mode}")
            self.state.derived["occupancy"] = mode
            self.state.bus.publish("total", None)
        return mode

    # -- generator run hours -------------------------------------------------------
    @property
    def generator_names(self) -> list[str]:
        return [g.name for g in self.cfg.generators] or ["Generator"]

    def active_generator(self) -> str:
        names = self.generator_names
        stored = self.store.get_kv(ACTIVE_GENERATOR_KEY)
        return stored if stored in names else names[0]

    def set_active_generator(self, name: str) -> str:
        if name not in self.generator_names:
            raise KeyError(f"unknown generator {name!r}; configured: {self.generator_names}")
        prev = self.active_generator()
        if prev != name:
            self.store.set_kv(ACTIVE_GENERATOR_KEY, name)
            self.store.add_event("info", "generator", f"cam switch set to {name}")
            self.state.derived["generator_active"] = name
            fc = self.state.forecast or {}
            if fc.get("plan"):
                from ..forecast.service import guard_for_cam_switch

                plan = fc["plan"]
                # undo a previous guard before re-applying it for the new selection
                if plan.get("manual_generator"):
                    plan["recommended_now"] = plan.pop("manual_start_now", False)
                    plan["stop_recommended"] = plan.pop("manual_stop_now", False)
                guard_for_cam_switch(plan, self.cfg, name)
                self.state.bus.publish("total", None)
        return name

    # -- fuel and oil ----------------------------------------------------------------
    def _wear_for(self, name: str) -> dict[str, float]:
        if self._wear is None:
            try:
                self._wear = json.loads(self.store.get_kv("generator_wear") or "{}")
            except ValueError:
                self._wear = {}
        return self._wear.setdefault(name, {"fuel": 0.0, "oil": 0.0})

    def _save_wear(self) -> None:
        if self._wear is not None:
            self.store.set_kv("generator_wear", json.dumps(self._wear))
            self._wear_saved = time.time()

    def _wear_summary(self, name: str) -> dict:
        g = self._generator_by_name(name)
        tank = g.tank_hours if g else 8.0
        oil = g.oil_change_h if g else 50.0
        w = self._wear_for(name)
        left = max(0.0, tank - w["fuel"])
        return {"fuel_left_h": round(left, 2), "fuel_pct": round(100.0 * left / tank) if tank > 0 else None,
                "tank_hours": tank, "oil_hours": round(w["oil"], 1), "oil_due_in_h": round(oil - w["oil"], 1),
                "oil_change_h": oil}

    def generator_service(self, name: str, what: str) -> dict:
        """Record a refill ("refilled") or an oil change ("oil_changed")."""
        if name not in self.generator_names:
            raise KeyError(f"unknown generator {name!r}")
        key = {"refilled": "fuel", "oil_changed": "oil"}[what]
        self._wear_for(name)[key] = 0.0
        self._save_wear()
        self.store.add_event("info", "generator", f"{name}: {'tank refilled' if key == 'fuel' else 'oil changed'}")
        return self._wear_summary(name)

    def set_generator_signal(self, name: str, running: bool | None = None, contactor: bool | None = None) -> dict:
        """Record an external signal (from Home Assistant via MQTT or REST)."""
        if name not in self.generator_names:
            raise KeyError(f"unknown generator {name!r}; configured: {self.generator_names}")
        sig = self.signals.setdefault(name, {"running": None, "contactor": None, "updated": 0.0})
        changed = False
        if running is not None and sig["running"] != running:
            sig["running"] = running
            changed = True
        if contactor is not None and sig["contactor"] != contactor:
            sig["contactor"] = contactor
            changed = True
        sig["updated"] = time.time()
        if changed:
            self.store.add_event("info", "generator", f"{name}: signal running={sig['running']} contactor={sig['contactor']}")
            self._track_generators()
            self.state.bus.publish("total", None)
        return dict(sig)

    def _generator_by_name(self, name: str):
        for g in self.cfg.generators:
            if g.name == name:
                return g
        return None

    def _inferred_running(self, now: float) -> bool:
        """Debounced 'Chargeverter is delivering power' from the BMS/inverter inference."""
        d = self.state.derived
        raw = bool((d.get("chargeverter_power") or 0) > 0)
        if raw != self._raw_running:
            self._raw_running, self._raw_since = raw, now
        held = now - self._raw_since
        prev = bool(d.get("_inferred_running_prev", False))
        if raw and not prev and held >= GEN_ON_DELAY_S:
            prev = True
        elif not raw and prev and held >= GEN_OFF_DELAY_S:
            prev = False
        d["_inferred_running_prev"] = prev
        return prev

    def _track_generators(self) -> None:
        """Per-generator running state and run hours.

        A generator with a fresh external signal uses it (engine hours, including
        warm-up before the contactor closes). Otherwise the cam-switch-selected
        generator is "running" while the Chargeverter is inferred to deliver power.
        """
        now = time.time()
        day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        names = self.generator_names
        if self._runtime_day != day:
            stored = self.store.daily_energy(2).get(day, {})
            self._runtime = {n: float(stored.get(runtime_metric(n), 0.0)) for n in names}
            self._runtime_day = day
            self._last_track = now
        dt_h = (now - (self._last_track or now)) / 3600.0
        self._last_track = now
        d = self.state.derived
        active = self.active_generator()
        delivering = self._inferred_running(now)
        gens = []
        any_running = False
        warnings: list[str] = []

        def fresh_signal(n: str) -> dict | None:
            cfg = self._generator_by_name(n)
            sig = self.signals.get(n)
            stale_s = cfg.signal_stale_s if cfg else 6 * 3600
            return sig if sig is not None and sig.get("running") is not None and now - sig["updated"] <= stale_s else None

        # Who is feeding the Chargeverter when only inference says "running"? The cam-switch
        # selection, unless that generator's own signal says it is off: then it must be one
        # without a signal (e.g. the porch generator while the outhouse sense wire reads off).
        supplier = None
        if delivering and not any((fresh_signal(n) or {}).get("running") for n in names):
            candidates = [n for n in names if fresh_signal(n) is None]
            supplier = active if active in candidates else (candidates[0] if candidates else None)
        for n in names:
            sig = self.signals.get(n)
            fresh = fresh_signal(n) is not None
            if fresh:
                running = bool(sig["running"])
                source = "signal"
                contactor = sig.get("contactor")
            else:
                running = delivering and n == supplier
                source = "inferred"
                contactor = None
            wear = self._wear_for(n)
            if running:
                self._runtime[n] = self._runtime.get(n, 0.0) + dt_h
                wear["fuel"] += dt_h
                wear["oil"] += dt_h
                any_running = True
                self._run_started.setdefault(n, now)
            else:
                self._run_started.pop(n, None)
            prev = self._running_prev.get(n)
            if prev is not None and prev != running:
                self.store.add_event("info", "generator", f"{n} {'started' if running else 'stopped'} ({source})")
            self._running_prev[n] = running
            warming_up = bool(running and source == "signal" and contactor is False)
            if running and source == "signal" and n != active:
                warnings.append(f"{n} is running but the cam switch is set to {active}.")
            if running and source == "signal" and contactor is True and not delivering and d.get("chargeverter_power") is not None:
                warnings.append(f"{n} contactor is closed but no Chargeverter output is seen.")
            gens.append({
                "name": n, "active": n == active, "running": running, "source": source,
                "contactor": contactor, "warming_up": warming_up, "delivering": bool(running and (delivering or (contactor and n == active))),
                "runtime_today_h": round(self._runtime.get(n, 0.0), 2),
                "run_elapsed_h": round((now - self._run_started[n]) / 3600.0, 2) if n in self._run_started else 0.0,
                **self._wear_summary(n),
                "signal_age_s": None if sig is None else round(now - sig["updated"]),
            })
        if any_running and now - self._wear_saved > 60:
            self._save_wear()
        d["generator_running"] = any_running
        d["generator_active"] = active
        d["generator_supplying"] = next((g["name"] for g in gens if g["running"]), None)
        d["generator_runtime_today_h"] = round(self._runtime.get(active, 0.0), 2)
        d["generators"] = gens
        d["generator_warnings"] = warnings
        if d.get("ac_input_live"):
            d["ac_input_warning"] = "The inverter's AC input is live, but nothing is expected to be connected there."

    def generator_summary(self) -> dict:
        today = {n: round(self._runtime.get(n, 0.0), 2) for n in self.generator_names}
        live = {g["name"]: g for g in self.state.derived.get("generators", [])}
        return {
            "active": self.active_generator(),
            "warnings": self.state.derived.get("generator_warnings", []),
            "generators": [
                {"name": g.name, "notes": g.notes, "runtime_today_h": today.get(g.name, 0.0),
                 "runtime_total_h": round(self.store.daily_sum(runtime_metric(g.name)), 1),
                 "signal_topics": {k: v for k, v in (("running", g.running_topic), ("contactor", g.contactor_topic)) if v},
                 "signal": self.signals.get(g.name),
                 "status": live.get(g.name),
                 "daily": self.store.daily_series(runtime_metric(g.name), 60)}
                for g in self.cfg.generators
            ],
        }

    # -- actions -----------------------------------------------------------------
    def write_setting_blocking(self, key: str, value: Any) -> dict:
        if self.inverter is None:
            raise RuntimeError("no inverter configured")
        result = self.inverter.write_setting(key, value)
        self.store.add_event("warn", "settings", f"{key} set to {value!r}")
        return result

    async def write_setting(self, key: str, value: Any) -> dict:
        return await asyncio.to_thread(self.write_setting_blocking, key, value)

    async def refresh_settings(self) -> dict:
        if self.inverter is None:
            raise RuntimeError("no inverter configured")
        return await asyncio.to_thread(self.inverter.refresh_settings)
