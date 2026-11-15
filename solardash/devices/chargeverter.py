"""Chargeverter GC bus task: passive sniffing, or answering as the battery."""

from __future__ import annotations

import asyncio
import logging
import time

from ..config import ChargeverterConfig
from ..core.derived import aggregate_batteries
from ..core.state import DeviceHealth, SystemState
from ..protocols import chargeverter as cv
from ..protocols.link import Link, SerialLink
from ..protocols.modbus_rtu import RtuFrameSplitter, parse_read_request

log = logging.getLogger(__name__)


class ChargeverterTask:
    def __init__(self, cfg: ChargeverterConfig, state: SystemState, link: Link | None = None) -> None:
        self.cfg = cfg
        self.state = state
        self.link = link or SerialLink(cfg.port, cfg.baud, parity=cfg.parity)
        self.health = DeviceHealth(name=cfg.name, kind="chargeverter", port=cfg.port)
        state.devices[f"chargeverter:{cfg.name}"] = self.health
        self.sniffer = cv.Sniffer()
        self.splitter = RtuFrameSplitter()
        self._stop = asyncio.Event()
        self.answered = 0
        self.refused_stale = 0
        self._last_publish = 0.0

    # -- the battery image we present -----------------------------------------
    def current_image(self) -> cv.BatteryImage | None:
        bank = aggregate_batteries(self.state.batteries, max_age_s=self.cfg.emulate.stale_after)
        if not bank.get("battery_count"):
            return None
        soc = bank.get("battery_state_of_charge")
        v = bank.get("battery_voltage")
        if soc is None or v is None:
            return None
        e = self.cfg.emulate
        return cv.BatteryImage(
            voltage=v,
            current=bank.get("battery_current", 0.0),
            soc=int(round(soc)) + e.soc_offset,
            soh=e.soh,
            temp_max=bank.get("battery_temperature_max", 25.0),
            temp_min=bank.get("battery_temperature_min", 25.0),
            capacity_ah=bank.get("battery_capacity_ah", 100.0),
            charge_limit_a=e.charge_limit_a,
            discharge_limit_a=e.discharge_limit_a,
            charge_voltage_v=e.charge_voltage,
            cycles=int(bank.get("battery_cycles_avg", 0)),
        )

    # -- blocking worker ---------------------------------------------------------
    def _service_once(self) -> None:
        data = self.link.read(64, 0.6)
        if not data:
            return
        for frame in self.splitter.feed(data):
            self.sniffer.on_frame(frame)
            if self.cfg.mode != "emulate":
                continue
            req = parse_read_request(frame)
            if req is None:
                continue
            image = self.current_image()
            if image is None:
                self.refused_stale += 1
                continue
            resp = cv.build_response(req, image)
            if resp:
                self.link.write(resp)
                self.answered += 1
                # our own reply also appears on the bus; account for it so the
                # sniffer's "battery answering" reflects reality
                self.sniffer.on_frame(resp)

    def _publish(self) -> None:
        d = self.sniffer.state.to_dict()
        d.update(mode=self.cfg.mode, answered=self.answered, refused_stale=self.refused_stale, port=self.cfg.port)
        img = self.current_image() if self.cfg.mode == "emulate" else None
        d["presented_soc"] = img.soc if img else None
        self.state.chargeverter = d
        self.health.connected = d["charger_online"]
        if d["charger_online"]:
            self.health.last_ok = time.time()
        self.health.poll_count = d["poll_count"]
        self.health.stats = {"polls": d["poll_count"], "replies": d["reply_count"], "answered": self.answered}
        self.state.bus.publish("chargeverter", None)

    async def run(self) -> None:
        if self.cfg.mode not in ("sniff", "emulate"):
            log.info("chargeverter task disabled (mode=%s)", self.cfg.mode)
            return
        log.info("chargeverter: %s mode on %s @ %d", self.cfg.mode, self.cfg.port, self.cfg.baud)
        if self.cfg.mode == "emulate":
            log.warning(
                "chargeverter: EMULATE mode - this process will answer the Chargeverter's battery polls with SOC %s; "
                "it stops answering when battery data is older than %.0fs",
                "from the JBD packs", self.cfg.emulate.stale_after,
            )
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._service_once)
                self.health.last_error = None
            except Exception as e:  # noqa: BLE001
                self.health.error_count += 1
                self.health.last_error = f"{type(e).__name__}: {e}"
                if self.health.error_count in (1, 5, 20) or self.health.error_count % 100 == 0:
                    log.warning("chargeverter: %s", self.health.last_error)
                self.link.close()
                await asyncio.sleep(min(5.0, 0.5 * self.health.error_count))
            now = time.monotonic()
            if now - self._last_publish >= 1.0:
                self._publish()
                self._last_publish = now

    def stop(self) -> None:
        self._stop.set()
