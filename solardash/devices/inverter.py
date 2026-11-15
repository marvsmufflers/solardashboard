"""EG4 6000XP poller (Luxpower Modbus RTU)."""

from __future__ import annotations

import logging
import threading
import time

from ..config import InverterConfig
from ..core.state import SystemState
from ..protocols import luxpower as lx
from ..protocols.link import Link, SerialLink
from ..protocols.modbus_rtu import ModbusException, ModbusRtuClient
from .base import Poller

log = logging.getLogger(__name__)


class InverterPoller(Poller):
    def __init__(self, cfg: InverterConfig, state: SystemState, link: Link | None = None) -> None:
        super().__init__(cfg.name, "inverter", cfg.port, state, cfg.poll_interval)
        self.cfg = cfg
        self.link = link or SerialLink(cfg.port, cfg.baud)
        self.client = ModbusRtuClient(self.link, timeout=cfg.timeout)
        self.unit = cfg.unit
        self._settings_due = 0.0
        self._unsupported_input_blocks: set[int] = set()
        self._unsupported_holding_blocks: set[int] = set()
        self._write_lock = threading.Lock()

    # -- polling ----------------------------------------------------------------
    def _read_blocks(self, fn, blocks, unsupported: set[int]) -> dict[int, int]:
        regs: dict[int, int] = {}
        got_any = False
        for start, count in blocks:
            if start in unsupported:
                continue
            try:
                values = fn(self.unit, start, count)
            except ModbusException as e:
                if e.code in (2, 3):  # illegal address / value: this model lacks the block
                    log.info("%s: block %d-%d not supported (%s); skipping from now on", self.name, start, start + count - 1, e)
                    unsupported.add(start)
                    continue
                raise
            got_any = True
            regs.update({start + i: v for i, v in enumerate(values)})
        if not got_any:
            raise RuntimeError("no register block could be read")
        return regs

    def poll_once(self) -> None:
        regs = self._read_blocks(self.client.read_input, lx.INPUT_BLOCKS, self._unsupported_input_blocks)
        data = lx.decode_inputs(regs)
        data["_updated"] = time.time()
        data["_raw"] = regs
        self.state.inverter = data
        self.state.inverter_updated = data["_updated"]
        self.health.stats = dict(self.client.stats)
        self.state.bus.publish("inverter", None)
        if time.monotonic() >= self._settings_due:
            self.refresh_settings()
            self._settings_due = time.monotonic() + self.cfg.settings_poll_interval

    def refresh_settings(self) -> dict:
        regs = self._read_blocks(self.client.read_holding, lx.HOLDING_BLOCKS, self._unsupported_holding_blocks)
        self.state.inverter_holding_raw = regs
        self.state.inverter_settings = lx.decode_settings(regs)
        self.state.settings_updated = time.time()
        self.state.bus.publish("settings", None)
        return self.state.inverter_settings

    # -- writes -----------------------------------------------------------------
    def write_setting(self, key: str, value) -> dict:
        """Blocking. Range-check, read-modify-write, write, read back, verify."""
        if not self.cfg.allow_writes:
            raise PermissionError("inverter.allow_writes is false in config")
        s = lx.SETTINGS_BY_KEY.get(key)
        if s is None:
            raise KeyError(f"unknown setting {key}")
        with self._write_lock:
            # fresh copy of the registers we touch (bit fields need the current value)
            fresh = {}
            for reg in {s.reg, s.end_reg} - {0}:
                fresh[reg] = self.client.read_holding(self.unit, reg, 1)[0]
            writes = lx.encode_setting_write(s, value, fresh)
            for reg, raw in writes:
                self.client.write_single(self.unit, reg, raw)
            time.sleep(0.1)
            for reg, raw in writes:
                back = self.client.read_holding(self.unit, reg, 1)[0]
                if back != raw:
                    raise RuntimeError(f"verify failed for register {reg}: wrote {raw}, read back {back}")
                self.state.inverter_holding_raw[reg] = back
            self.state.inverter_settings = lx.decode_settings(self.state.inverter_holding_raw)
            self.state.settings_updated = time.time()
            self.state.bus.publish("settings", None)
            log.warning("%s: setting %s changed to %r (registers %s)", self.name, key, value, writes)
            return {key: self.state.inverter_settings.get(key)}

    def on_disconnect(self) -> None:
        self.link.close()
