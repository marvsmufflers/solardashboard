"""EG4 Chargeverter GC battery-bus protocol.

The Chargeverter has no monitoring interface of its own. Its RJ45 "BMS" port
(pins 1 = RS485-B, 2 = RS485-A) is a **Modbus RTU master** that polls an EG4
battery at 9600 8N1 for 17 holding registers starting at 0x0013 and uses the
SOC it receives to start and stop the generator. Observed poll frame (every
~500 ms):

    01 03 00 13 00 11 74 03

Two things are therefore possible from software:

* **Sniff** the bus passively (adapter wired A/B in parallel) to learn whether
  the Chargeverter is powered and polling, and what SOC it is being told.
* **Emulate** the battery, answering those polls with live data aggregated
  from the real (JBD) batteries, so the Chargeverter shows a real SOC instead
  of "LOST" and can start/stop the generator on SOC.

Register image (empirically verified by the yambms-chargeverter and eg4mimic
projects; only SOC and voltage are *confirmed* to be used by the charger):

    0x13 reserved             0x1C capacity, 0.01 Ah
    0x14 current, 0.1 A signed 0x1D charge current limit, 0.1 A
    0x15 SOC %  (confirmed)    0x1E discharge current limit, 0.1 A
    0x16 voltage, 0.01 V (conf.) 0x1F charge voltage limit, 0.01 V
    0x17 charge current limit  0x20 cycles low word
    0x18 SOH %                 0x21 cycles high word
    0x19 SOC % (mirror)        0x22 reserved
    0x1A max temp, 0.1 °C      0x23 reserved
    0x1B min temp, 0.1 °C
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

from .modbus_rtu import (
    READ_HOLDING,
    ReadRequest,
    build_read_response,
    parse_read_request,
)

BAUD = 9600
UNIT = 0x01
POLL_START = 0x0013
POLL_COUNT = 0x0011  # 17 registers, 0x13..0x23
POLL_FRAME = bytes.fromhex("01 03 00 13 00 11 74 03")


@dataclass
class BatteryImage:
    """What we tell the Chargeverter about the battery bank."""

    voltage: float  # V
    current: float  # A, positive = charging
    soc: int  # %
    soh: int = 100
    temp_max: float = 25.0
    temp_min: float = 25.0
    capacity_ah: float = 100.0
    charge_limit_a: float = 100.0
    discharge_limit_a: float = 100.0
    charge_voltage_v: float = 56.0
    cycles: int = 0

    def registers(self) -> dict[int, int]:
        def s16(x: float) -> int:
            return struct.unpack(">H", struct.pack(">h", int(round(x))))[0]

        soc = max(0, min(100, int(self.soc)))
        regs = {
            0x13: 0,
            0x14: s16(self.current * 10),
            0x15: soc,
            0x16: int(round(self.voltage * 100)) & 0xFFFF,
            0x17: int(round(self.charge_limit_a * 10)) & 0xFFFF,
            0x18: max(0, min(100, int(self.soh))),
            0x19: soc,
            0x1A: s16(self.temp_max * 10),
            0x1B: s16(self.temp_min * 10),
            0x1C: int(round(self.capacity_ah * 100)) & 0xFFFF,
            0x1D: int(round(self.charge_limit_a * 10)) & 0xFFFF,
            0x1E: int(round(self.discharge_limit_a * 10)) & 0xFFFF,
            0x1F: int(round(self.charge_voltage_v * 100)) & 0xFFFF,
            0x20: self.cycles & 0xFFFF,
            0x21: (self.cycles >> 16) & 0xFFFF,
            0x22: 0,
            0x23: 0,
        }
        return regs


def build_response(req: ReadRequest, image: BatteryImage) -> bytes | None:
    """Answer a Chargeverter poll. Returns None for requests we should ignore."""
    if req.unit != UNIT or req.function != READ_HOLDING:
        return None
    if req.address < POLL_START or req.address + req.count > POLL_START + POLL_COUNT:
        return None
    regs = image.registers()
    values = [regs.get(a, 0) for a in range(req.address, req.address + req.count)]
    return build_read_response(req.unit, req.function, values)


@dataclass
class SniffedReply:
    soc: int | None = None
    voltage: float | None = None
    current: float | None = None
    soh: int | None = None
    temp_max: float | None = None
    temp_min: float | None = None
    raw: list[int] = field(default_factory=list)


def decode_reply_registers(values: list[int], start: int = POLL_START) -> SniffedReply:
    regs = {start + i: v for i, v in enumerate(values)}

    def s16(v: int) -> int:
        return struct.unpack(">h", struct.pack(">H", v))[0]

    r = SniffedReply(raw=values)
    if 0x15 in regs:
        r.soc = regs[0x15]
    if 0x16 in regs:
        r.voltage = round(regs[0x16] / 100.0, 2)
    if 0x14 in regs:
        r.current = round(s16(regs[0x14]) / 10.0, 1)
    if 0x18 in regs:
        r.soh = regs[0x18]
    if 0x1A in regs:
        r.temp_max = round(s16(regs[0x1A]) / 10.0, 1)
    if 0x1B in regs:
        r.temp_min = round(s16(regs[0x1B]) / 10.0, 1)
    return r


@dataclass
class SnifferState:
    last_poll_at: float | None = None
    poll_count: int = 0
    reply_count: int = 0
    last_poll_address: int | None = None
    last_poll_quantity: int | None = None
    last_reply: SniffedReply | None = None
    last_reply_at: float | None = None
    other_frames: int = 0

    @property
    def charger_online(self) -> bool:
        return self.last_poll_at is not None and (time.monotonic() - self.last_poll_at) < 3.0

    @property
    def battery_answering(self) -> bool:
        return self.last_reply_at is not None and (time.monotonic() - self.last_reply_at) < 3.0

    def to_dict(self) -> dict:
        now = time.monotonic()
        return {
            "charger_online": self.charger_online,
            "battery_answering": self.battery_answering,
            "poll_count": self.poll_count,
            "reply_count": self.reply_count,
            "seconds_since_poll": None if self.last_poll_at is None else round(now - self.last_poll_at, 1),
            "last_poll_address": self.last_poll_address,
            "last_poll_quantity": self.last_poll_quantity,
            "last_reply": None
            if self.last_reply is None
            else {
                "soc": self.last_reply.soc,
                "voltage": self.last_reply.voltage,
                "current": self.last_reply.current,
                "soh": self.last_reply.soh,
                "temp_max": self.last_reply.temp_max,
                "temp_min": self.last_reply.temp_min,
            },
            "other_frames": self.other_frames,
        }


class Sniffer:
    """Feed it Modbus RTU frames seen on the Chargeverter bus."""

    def __init__(self) -> None:
        self.state = SnifferState()
        self._pending: ReadRequest | None = None

    def on_frame(self, frame: bytes) -> None:
        req = parse_read_request(frame)
        now = time.monotonic()
        if req is not None and req.unit == UNIT and req.function == READ_HOLDING:
            self.state.poll_count += 1
            self.state.last_poll_at = now
            self.state.last_poll_address = req.address
            self.state.last_poll_quantity = req.count
            self._pending = req
            return
        # a reply to the pending request?
        if self._pending is not None and len(frame) >= 5 and frame[0] == UNIT and frame[1] == READ_HOLDING:
            n = frame[2]
            if len(frame) == 5 + n and n == 2 * self._pending.count:
                values = list(struct.unpack(f">{n // 2}H", frame[3 : 3 + n]))
                self.state.last_reply = decode_reply_registers(values, self._pending.address)
                self.state.last_reply_at = now
                self.state.reply_count += 1
                self._pending = None
                return
        self.state.other_frames += 1
