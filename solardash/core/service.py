"""Wires config -> devices -> state -> history/MQTT/API."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from ..config import Config
from ..devices.battery import BatteryPoller
from ..devices.chargeverter import ChargeverterTask
from ..devices.inverter import InverterPoller
from ..mqtt.publisher import MqttPublisher
from ..storage.sqlite import HistoryStore
from .derived import compute_totals
from .state import SystemState

log = logging.getLogger(__name__)

# Which inverter keys go to history (bounded DB growth).
INVERTER_HISTORY_KEYS = [
    "pv_power", "pv1_power", "pv2_power", "pv1_voltage", "pv2_voltage", "battery_power", "battery_voltage",
    "battery_soc", "load_power", "eps_power", "eps_l1_power", "eps_l2_power", "eps_voltage", "eps_frequency",
    "grid_power", "grid_voltage", "grid_frequency", "generator_power", "rectifier_power", "temperature_inner",
    "temperature_radiator_1", "temperature_radiator_2", "temperature_battery", "bms_battery_current",
    "bus_voltage_1", "status_code", "fault_code", "warning_code",
]
TOTAL_HISTORY_KEYS = [
    "battery_power", "battery_voltage", "battery_current", "battery_state_of_charge", "battery_temperature",
    "battery_cell_voltage_min", "battery_cell_voltage_max", "battery_cell_imbalance_max", "chargeverter_power",
    "chargeverter_power_raw", "pv_power", "load_power", "grid_power", "generator_power",
]
BATTERY_HISTORY_KEYS = ["voltage", "current", "power", "soc", "temperature", "cell_min", "cell_max", "cell_imbalance"]
DAILY_ENERGY_KEYS = [
    "pv_energy_today", "load_energy_today", "battery_charge_energy_today", "battery_discharge_energy_today",
    "grid_import_energy_today", "grid_export_energy_today", "generator_energy_today", "eps_energy_today",
]


class Service:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.state = SystemState()
        self.store = HistoryStore(cfg.history.path, cfg.history.retention_days)
        self.inverter: InverterPoller | None = None
        self.batteries: list[BatteryPoller] = []
        self.chargeverter: ChargeverterTask | None = None
        self.mqtt: MqttPublisher | None = None
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._build()

    # -- construction ------------------------------------------------------------
    def _build(self) -> None:
        cfg = self.cfg
        world = None
        if cfg.simulate:
            from ..devices.simulated import (
                SimulatedBatteryLink,
                SimulatedChargeverterLink,
                SimulatedInverterLink,
                SimWorld,
            )

            world = SimWorld(packs=max(1, len(cfg.batteries)))
            log.warning("SIMULATION MODE: no real hardware is being read")
        if cfg.inverter.enabled:
            link = SimulatedInverterLink(world, cfg.inverter.unit) if world else None
            self.inverter = InverterPoller(cfg.inverter, self.state, link=link)
        for i, b in enumerate(cfg.batteries, 1):
            if not b.enabled:
                continue
            link = SimulatedBatteryLink(world, i - 1) if world else None
            self.batteries.append(BatteryPoller(i, b, self.state, link=link))
        if cfg.chargeverter.enabled and cfg.chargeverter.mode in ("sniff", "emulate"):
            link = SimulatedChargeverterLink(world, with_battery=(cfg.chargeverter.mode == "sniff")) if world else None
            self.chargeverter = ChargeverterTask(cfg.chargeverter, self.state, link=link)
        if cfg.mqtt.enabled:
            self.mqtt = MqttPublisher(cfg.mqtt, self.state, write_setting=self.write_setting_blocking)

    # -- lifecycle -----------------------------------------------------------------
    async def start(self) -> None:
        if self.inverter:
            self._tasks.append(asyncio.create_task(self.inverter.run(), name="inverter"))
        for b in self.batteries:
            self._tasks.append(asyncio.create_task(b.run(), name=f"battery{b.index}"))
        if self.chargeverter:
            self._tasks.append(asyncio.create_task(self.chargeverter.run(), name="chargeverter"))
        if self.mqtt:
            self._tasks.append(asyncio.create_task(self.mqtt.run(), name="mqtt"))
        self._tasks.append(asyncio.create_task(self._derive_loop(), name="derive"))
        self._tasks.append(asyncio.create_task(self._sample_loop(), name="sample"))
        self.store.add_event("info", "service", "solardash started" + (" (simulation)" if self.cfg.simulate else ""))

    async def stop(self) -> None:
        self._stop.set()
        if self.inverter:
            self.inverter.stop()
        for b in self.batteries:
            b.stop()
        if self.chargeverter:
            self.chargeverter.stop()
        if self.mqtt:
            self.mqtt.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
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
                await asyncio.to_thread(self.store.maybe_rollup)
            except Exception as e:  # noqa: BLE001
                log.exception("sample loop: %s", e)
            await asyncio.sleep(interval)

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
