"""Simulated hardware so the whole app runs without a single serial port.

One ``SimWorld`` keeps the physics consistent across devices: PV follows the
time of day, a house load wanders, the three packs share the bus current, and
a simulated Chargeverter starts "the generator" when the SOC it is told drops
below 50 % and stops above 90 %.
"""

from __future__ import annotations

import math
import random
import struct
import time

from ..protocols import chargeverter as cv
from ..protocols import jbd
from ..protocols.crc import check_crc
from ..protocols.link import LoopbackLink
from ..protocols.modbus_rtu import (
    READ_HOLDING,
    READ_INPUT,
    WRITE_MULTIPLE,
    WRITE_SINGLE,
    build_exception_response,
    build_read_response,
    parse_read_request,
)
from ..protocols.crc import append_crc


class SimWorld:
    def __init__(self, packs: int = 3, speed: float = 1.0, seed: int = 7) -> None:
        self.rng = random.Random(seed)
        self.packs = packs
        self.speed = speed  # >1 makes the day go faster
        self.t0 = time.time()
        self.soc = [0.62, 0.60, 0.64][:packs] + [0.6] * max(0, packs - 3)
        self.capacity_ah = 100.0
        self.load_w = 600.0
        self.gen_on = False
        self.cv_told_soc: int | None = None
        self.holding: dict[int, int] = self._default_holding()
        self._last = time.time()

    # -- environment -------------------------------------------------------------
    def sim_time(self) -> float:
        return self.t0 + (time.time() - self.t0) * self.speed

    def hour_of_day(self) -> float:
        t = time.localtime(self.sim_time())
        return t.tm_hour + t.tm_min / 60 + t.tm_sec / 3600

    def pv_w(self) -> float:
        h = self.hour_of_day()
        if 6.5 < h < 18.5:
            base = 4800 * math.sin(math.pi * (h - 6.5) / 12.0)
            cloud = 0.75 + 0.25 * math.sin(self.sim_time() / 97.0)
            return max(0.0, base * cloud)
        return 0.0

    def step(self) -> None:
        now = time.time()
        dt = (now - self._last) * self.speed
        self._last = now
        self.load_w = max(150.0, min(5500.0, self.load_w + self.rng.gauss(0, 60)))
        pv = self.pv_w()
        v = self.pack_voltage()
        inv_batt_w = pv * 0.96 - self.load_w / 0.93  # + charging
        cv_w = 4200.0 if self.gen_on else 0.0
        bank_w = inv_batt_w + cv_w
        bank_ah = bank_w / v * dt / 3600.0
        # distribute by inverse SOC so packs converge
        weights = [1.05 - s for s in self.soc]
        wsum = sum(weights)
        for i in range(self.packs):
            d = bank_ah * weights[i] / wsum / self.capacity_ah
            self.soc[i] = max(0.03, min(1.0, self.soc[i] + d))
        self._inv_batt_w = inv_batt_w
        self._cv_w = cv_w
        # chargeverter logic based on what it was told
        if self.cv_told_soc is not None:
            if self.cv_told_soc <= 50:
                self.gen_on = True
            elif self.cv_told_soc >= 90:
                self.gen_on = False

    def pack_voltage(self, i: int | None = None) -> float:
        soc = sum(self.soc) / self.packs if i is None else self.soc[i]
        # crude LFP curve
        return round(48.0 + 5.6 * soc + (0.6 if soc > 0.97 else 0.0), 2)

    def pack_current(self, i: int) -> float:
        v = self.pack_voltage()
        total = (getattr(self, "_inv_batt_w", 0.0) + getattr(self, "_cv_w", 0.0)) / v
        weights = [1.05 - s for s in self.soc]
        return round(total * weights[i] / sum(weights) + self.rng.gauss(0, 0.05), 2)

    # -- inverter register images -----------------------------------------------
    def _default_holding(self) -> dict[int, int]:
        h = {a: 0 for a in range(0, 240)}
        h[0], h[1] = 0x0906, 0x1AC1
        sn = b"3473740399"
        for k in range(5):
            h[2 + k] = struct.unpack("<H", sn[2 * k : 2 * k + 2])[0]
        h[15] = 1
        h[19] = 740
        h[21] = (1 << 9) | (1 << 0) | (1 << 8) | (1 << 4) | (1 << 5)  # on, eps, seamless, anti-island, neutral
        h[64], h[65], h[66], h[67] = 100, 100, 50, 50
        h[90], h[91] = 240, 60
        h[99], h[100], h[101], h[102] = 574, 464, 100, 100
        h[103], h[105] = 100, 20
        h[110] = 1 << 7
        h[120] = (3 << 1) | (1 << 4) | (1 << 6) | (1 << 7)
        h[125] = 20
        h[144], h[145], h[146], h[147], h[148] = 540, 0, 2, 300, 512
        h[158], h[159], h[160], h[161] = 464, 564, 20, 50
        h[162], h[163], h[164], h[165], h[166], h[167], h[168], h[169] = 460, 500, 10, 20, 464, 10, 0, 480
        h[177], h[179] = 6000, 0
        h[194], h[195], h[196], h[197], h[198] = 560, 564, 50, 90, 11
        h[201], h[202] = 564, 480
        return h

    def input_regs(self) -> dict[int, int]:
        self.step()
        r = {a: 0 for a in range(0, 200)}
        pv = self.pv_w()
        v = self.pack_voltage()
        soc = int(round(100 * sum(self.soc) / self.packs))
        inv_w = self._inv_batt_w
        r[0] = (0x88 if pv > 0 and inv_w > 0 else 0x80 if pv > 0 else 0x40)
        r[1] = int(round((230 + 60 * (pv > 0)) * 10))
        r[2] = int(round((225 + 55 * (pv > 0)) * 10))
        r[4] = int(round(v * 10))
        r[5] = soc | (100 << 8)
        r[7] = int(round(pv * 0.55))
        r[8] = int(round(pv * 0.45))
        r[10] = int(max(0.0, inv_w))
        r[11] = int(max(0.0, -inv_w))
        r[15] = 0
        r[16] = int(self.load_w / 0.93)
        r[20] = 2401
        r[23] = 6001
        r[24] = int(self.load_w)
        r[25] = int(self.load_w * 1.05)
        h = self.hour_of_day()
        r[28] = int(min(h, 18.5) * 1.2 * 10) if h > 6.5 else 0
        r[29] = int(r[28] * 0.8)
        r[33] = int(h * 0.3 * 10)
        r[34] = int(h * 0.5 * 10)
        r[35] = int(h * 0.7 * 10)
        r[38], r[39] = 3851, 3850
        for k, val in ((40, 12345.6), (46, 9876.5), (50, 4567.8), (52, 4321.0), (54, 8765.4)):
            raw = int(val * 10)
            r[k], r[k + 1] = raw & 0xFFFF, raw >> 16
        r[64], r[65], r[66], r[67] = 22, 21, 23, 19
        rt = int(time.time() - self.t0) + 86400 * 40
        r[69], r[70] = rt & 0xFFFF, rt >> 16
        r[77] = 0
        r[80] = 1
        r[81], r[82] = 3000, 3000
        r[83], r[84] = 564, 464
        r[96], r[97] = self.packs, int(self.capacity_ah * self.packs)
        total_i = (self._inv_batt_w) / v
        r[98] = int(round(total_i * 10)) & 0xFFFF
        r[101], r[102] = 3261, 3252
        r[103], r[104] = 183, 131
        r[106] = 100
        r[107] = int(round(v * 10))
        r[120] = 1925
        r[127], r[128] = 1196, 1196
        r[129] = int(self.load_w * 0.55)
        r[130] = int(self.load_w * 0.45)
        r[170] = int(self.load_w)
        r[171] = int(h * 0.7 * 10)
        return r

    # -- battery frames ----------------------------------------------------------
    def battery_basic_info(self, i: int) -> bytes:
        self.step()
        soc = self.soc[i]
        temps = [round(14 + 3 * soc + self.rng.gauss(0, 0.3), 1) for _ in range(6)]
        return jbd.encode_basic_info(
            voltage=self.pack_voltage(i),
            current=self.pack_current(i),
            remaining_ah=round(self.capacity_ah * soc, 2),
            nominal_ah=self.capacity_ah,
            cycles=91 + i,
            soc=int(round(soc * 100)),
            temperatures=temps,
        )

    def battery_cells(self, i: int) -> bytes:
        v = self.pack_voltage(i) / 16
        return b"".join(struct.pack(">H", int(round((v + self.rng.gauss(0, 0.002)) * 1000))) for _ in range(16))


class SimulatedInverterLink(LoopbackLink):
    def __init__(self, world: SimWorld, unit: int = 1) -> None:
        super().__init__(self._respond)
        self.world = world
        self.unit = unit

    def _respond(self, req: bytes) -> bytes:
        if len(req) < 8 or not check_crc(req) or req[0] != self.unit:
            return b""
        fn = req[1]
        if fn in (READ_HOLDING, READ_INPUT):
            r = parse_read_request(req)
            assert r is not None
            if r.address + r.count > 240:
                return build_exception_response(self.unit, fn, 2)
            image = self.world.holding if fn == READ_HOLDING else self.world.input_regs()
            return build_read_response(self.unit, fn, [image.get(a, 0) for a in range(r.address, r.address + r.count)])
        if fn == WRITE_SINGLE:
            addr, val = struct.unpack(">HH", req[2:6])
            self.world.holding[addr] = val
            return req
        if fn == WRITE_MULTIPLE:
            addr, cnt, nb = struct.unpack(">HHB", req[2:7])
            vals = struct.unpack(f">{cnt}H", req[7 : 7 + nb])
            for k, v in enumerate(vals):
                self.world.holding[addr + k] = v
            return append_crc(req[:6])
        return build_exception_response(self.unit, fn, 1)


class SimulatedBatteryLink(LoopbackLink):
    def __init__(self, world: SimWorld, index: int) -> None:
        super().__init__(self._respond)
        self.world = world
        self.index = index

    def _respond(self, req: bytes) -> bytes:
        if len(req) < 7 or req[0] != jbd.START:
            return b""
        cmd = req[2]
        if cmd == jbd.CMD_BASIC_INFO:
            return jbd.build_reply(cmd, self.world.battery_basic_info(self.index))
        if cmd == jbd.CMD_CELL_VOLTAGES:
            return jbd.build_reply(cmd, self.world.battery_cells(self.index))
        if cmd == jbd.CMD_HW_VERSION:
            return jbd.build_reply(cmd, b"SIM-UP16S015")
        return jbd.build_reply(cmd, b"", status=0x80)


class SimulatedChargeverterLink:
    """Acts as the Chargeverter (Modbus master) on the bus.

    * ``read`` yields a poll roughly every 0.5 s. In *sniff* simulation a fake
      battery reply follows each poll.
    * ``write`` captures our reply (emulate mode) and feeds its SOC into the
      world so the simulated generator reacts.
    """

    def __init__(self, world: SimWorld, with_battery: bool = False) -> None:
        self.world = world
        self.with_battery = with_battery
        self._next_poll = time.monotonic() + 0.3
        self._pending: bytes | None = None

    def read(self, max_bytes: int, timeout: float) -> bytes:
        if self._pending is not None:
            out, self._pending = self._pending, None
            time.sleep(0.02)
            return out
        wait = self._next_poll - time.monotonic()
        if wait > timeout:
            time.sleep(timeout)
            return b""
        if wait > 0:
            time.sleep(wait)
        self._next_poll = time.monotonic() + 0.5
        if self.with_battery:
            self.world.step()
            soc = int(round(100 * sum(self.world.soc) / self.world.packs))
            img = cv.BatteryImage(voltage=self.world.pack_voltage(), current=0.0, soc=soc)
            regs = img.registers()
            self._pending = build_read_response(1, READ_HOLDING, [regs[a] for a in range(0x13, 0x24)])
            self.world.cv_told_soc = soc
        return cv.POLL_FRAME

    def write(self, data: bytes) -> None:
        if len(data) >= 5 and data[0] == 1 and data[1] == READ_HOLDING and check_crc(data):
            n = data[2]
            vals = struct.unpack(f">{n // 2}H", data[3 : 3 + n])
            self.world.cv_told_soc = vals[0x15 - 0x13]

    def flush_input(self) -> None:
        pass

    def close(self) -> None:
        pass
