"""JBD battery poller (one per RS485 link)."""

from __future__ import annotations

import logging
import time

from ..config import BatteryConfig
from ..core.state import SystemState
from ..protocols.jbd import JbdClient
from ..protocols.link import Link, SerialLink
from .base import Poller

log = logging.getLogger(__name__)


class BatteryPoller(Poller):
    def __init__(self, index: int, cfg: BatteryConfig, state: SystemState, link: Link | None = None) -> None:
        super().__init__(cfg.name, "battery", cfg.port, state, cfg.poll_interval)
        self.index = index
        self.cfg = cfg
        self.link = link or SerialLink(cfg.port, cfg.baud)
        self.client = JbdClient(self.link, timeout=cfg.timeout)
        self._hw_version: str | None = None

    def poll_once(self) -> None:
        info = self.client.basic_info()
        time.sleep(0.05)
        cells = self.client.cell_voltages()
        if self._hw_version is None:
            try:
                time.sleep(0.05)
                self._hw_version = self.client.hw_version()
            except Exception as e:  # noqa: BLE001
                log.debug("%s: hw version unavailable: %s", self.name, e)
                self._hw_version = ""
        data = {
            "name": self.cfg.name,
            "voltage": info.voltage,
            "current": info.current,
            "power": info.power,
            "soc": info.soc,
            "remaining_ah": info.remaining_ah,
            "nominal_ah": info.nominal_ah or self.cfg.capacity_ah,
            "cycles": info.cycles,
            "temperatures": info.temperatures,
            "temperature": round(sum(info.temperatures) / len(info.temperatures), 1) if info.temperatures else None,
            "cells": cells,
            "cell_count": len(cells),
            "cell_min": min(cells) if cells else None,
            "cell_max": max(cells) if cells else None,
            "cell_avg": round(sum(cells) / len(cells), 4) if cells else None,
            "cell_imbalance": round(max(cells) - min(cells), 3) if cells else None,
            "balancing": [i + 1 for i, b in enumerate(info.balancing) if b],
            "protection": info.protection,
            "protection_bits": info.protection_bits,
            "charge_fet_on": info.charge_fet_on,
            "discharge_fet_on": info.discharge_fet_on,
            "software_version": info.software_version,
            "hardware_version": self._hw_version or None,
            "production_date": info.production_date,
            "_updated": time.time(),
        }
        self.state.batteries[self.index] = data
        self.state.battery_updated[self.index] = data["_updated"]
        self.health.stats = dict(self.client.stats)
        self.state.bus.publish("battery", self.index)

    def on_disconnect(self) -> None:
        self.link.close()
