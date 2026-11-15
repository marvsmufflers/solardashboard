"""JBD (Jiabaida / Xiaoxiang) BMS protocol.

Used by the Eco-Worthy 48V 100Ah server-rack batteries (JBD UP16S015 board),
reachable on the battery's RS485-1 port through a USB-RS485 adapter. This is
the protocol Solar Assistant calls "USB JBD RS485".

Frame:  DD | A5(read)/5A(write) | cmd | len | data... | chk_hi chk_lo | 77
Checksum: two's complement of the sum of (cmd, len, data) bytes, 16-bit BE.
Reply:  DD | cmd | status(00=ok) | len | data... | chk_hi chk_lo | 77
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

START = 0xDD
END = 0x77
READ = 0xA5
WRITE = 0x5A

CMD_BASIC_INFO = 0x03
CMD_CELL_VOLTAGES = 0x04
CMD_HW_VERSION = 0x05

PROTECTION_BITS = {
    0: "Cell over-voltage",
    1: "Cell under-voltage",
    2: "Pack over-voltage",
    3: "Pack under-voltage",
    4: "Charge over-temperature",
    5: "Charge under-temperature",
    6: "Discharge over-temperature",
    7: "Discharge under-temperature",
    8: "Charge over-current",
    9: "Discharge over-current",
    10: "Short circuit",
    11: "Front-end IC error",
    12: "MOSFET software lock",
}


class JbdError(Exception):
    pass


def checksum(payload: bytes) -> int:
    """Checksum over cmd+len+data (request) or status+len+data (reply)."""
    return (0x10000 - (sum(payload) & 0xFFFF)) & 0xFFFF


def build_request(cmd: int, data: bytes = b"", write: bool = False) -> bytes:
    body = bytes((cmd, len(data))) + data
    chk = checksum(body)
    return bytes((START, WRITE if write else READ)) + body + struct.pack(">H", chk) + bytes((END,))


REQ_BASIC_INFO = build_request(CMD_BASIC_INFO)
REQ_CELL_VOLTAGES = build_request(CMD_CELL_VOLTAGES)
REQ_HW_VERSION = build_request(CMD_HW_VERSION)


def parse_reply(frame: bytes) -> tuple[int, bytes]:
    """Validate a reply frame and return (cmd, data)."""
    if len(frame) < 7:
        raise JbdError(f"short frame ({len(frame)} bytes)")
    if frame[0] != START or frame[-1] != END:
        raise JbdError(f"bad framing: {frame.hex(' ')}")
    cmd, status, n = frame[1], frame[2], frame[3]
    if len(frame) != 7 + n:
        raise JbdError(f"length mismatch: header says {n}, frame has {len(frame) - 7}")
    data = frame[4 : 4 + n]
    chk = struct.unpack(">H", frame[4 + n : 6 + n])[0]
    if chk != checksum(frame[2 : 4 + n]):
        raise JbdError(f"bad checksum: {frame.hex(' ')}")
    if status != 0:
        raise JbdError(f"BMS returned error status 0x{status:02X} for cmd 0x{cmd:02X}")
    return cmd, data


def expected_reply_length(first4: bytes) -> int | None:
    """Given the first 4 bytes of a reply, return the full frame length."""
    if len(first4) < 4 or first4[0] != START:
        return None
    return 7 + first4[3]


@dataclass
class BasicInfo:
    voltage: float  # V
    current: float  # A, positive = charging
    remaining_ah: float
    nominal_ah: float
    cycles: int
    production_date: str
    balancing: list[bool]  # per cell
    protection_bits: int
    protection: list[str]
    software_version: str
    soc: int
    charge_fet_on: bool
    discharge_fet_on: bool
    cell_count: int
    temperatures: list[float] = field(default_factory=list)  # °C

    @property
    def power(self) -> float:
        return round(self.voltage * self.current, 1)


def decode_basic_info(data: bytes) -> BasicInfo:
    if len(data) < 23:
        raise JbdError(f"basic info too short ({len(data)} bytes)")
    (v, i, rem, nom, cycles, date, bal_lo, bal_hi, prot) = struct.unpack(">HhHHHHHHH", data[:18])
    sw = data[18]
    soc = data[19]
    fet = data[20]
    ncell = data[21]
    ntc_count = data[22]
    # bit 7 of the FET/status byte means current and capacity are in 0.1 units
    cur_scale = 0.1 if fet & 0x80 else 0.01
    temps: list[float] = []
    for k in range(ntc_count):
        off = 23 + 2 * k
        if off + 2 <= len(data):
            raw = struct.unpack(">H", data[off : off + 2])[0]
            temps.append(round((raw - 2731) / 10.0, 1))
    balance_mask = bal_lo | (bal_hi << 16)
    year = 2000 + (date >> 9)
    month = (date >> 5) & 0x0F
    day = date & 0x1F
    return BasicInfo(
        voltage=round(v / 100.0, 2),
        current=round(i * cur_scale, 2),
        remaining_ah=round(rem * cur_scale, 2),
        nominal_ah=round(nom * cur_scale, 2),
        cycles=cycles,
        production_date=f"{year:04d}-{month:02d}-{day:02d}",
        balancing=[bool(balance_mask >> c & 1) for c in range(max(ncell, 1))],
        protection_bits=prot,
        protection=[name for bit, name in PROTECTION_BITS.items() if prot >> bit & 1],
        software_version=f"{sw >> 4}.{sw & 0x0F}",
        soc=soc,
        charge_fet_on=bool(fet & 0x01),
        discharge_fet_on=bool(fet & 0x02),
        cell_count=ncell,
        temperatures=temps,
    )


def decode_cell_voltages(data: bytes) -> list[float]:
    if len(data) % 2 or not 2 <= len(data) <= 64:
        raise JbdError(f"bad cell voltage payload ({len(data)} bytes)")
    return [round(x / 1000.0, 3) for x in struct.unpack(f">{len(data) // 2}H", data)]


def decode_hw_version(data: bytes) -> str:
    return data.decode("ascii", errors="replace").strip("\x00 ")


class JbdClient:
    """Blocking JBD client over a :class:`Link`."""

    def __init__(self, link, timeout: float = 1.0, retries: int = 2) -> None:
        self.link = link
        self.timeout = timeout
        self.retries = retries
        self.stats = {"requests": 0, "timeouts": 0, "errors": 0}

    def _exchange(self, request: bytes) -> tuple[int, bytes]:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            self.link.flush_input()
            self.link.write(request)
            self.stats["requests"] += 1
            head = self.link.read(4, self.timeout)
            if len(head) < 4:
                self.stats["timeouts"] += 1
                last = JbdError("no response")
            else:
                total = expected_reply_length(head)
                if total is None:
                    last = JbdError(f"bad reply header {head.hex(' ')}")
                    self.stats["errors"] += 1
                else:
                    rest = self.link.read(total - 4, self.timeout) if total > 4 else b""
                    try:
                        return parse_reply(head + rest)
                    except JbdError as e:
                        self.stats["errors"] += 1
                        last = e
            if attempt < self.retries:
                import time

                time.sleep(0.1)
        assert last is not None
        raise last

    def basic_info(self) -> BasicInfo:
        cmd, data = self._exchange(REQ_BASIC_INFO)
        if cmd != CMD_BASIC_INFO:
            raise JbdError(f"unexpected cmd 0x{cmd:02X}")
        return decode_basic_info(data)

    def cell_voltages(self) -> list[float]:
        cmd, data = self._exchange(REQ_CELL_VOLTAGES)
        if cmd != CMD_CELL_VOLTAGES:
            raise JbdError(f"unexpected cmd 0x{cmd:02X}")
        return decode_cell_voltages(data)

    def hw_version(self) -> str:
        cmd, data = self._exchange(REQ_HW_VERSION)
        return decode_hw_version(data)


def build_reply(cmd: int, data: bytes, status: int = 0) -> bytes:
    """Build a BMS reply frame (used by the simulator)."""
    body = bytes((status, len(data))) + data
    return bytes((START, cmd)) + body + struct.pack(">H", checksum(body)) + bytes((END,))


def encode_basic_info(
    voltage: float,
    current: float,
    remaining_ah: float,
    nominal_ah: float,
    cycles: int,
    soc: int,
    temperatures: list[float],
    cell_count: int = 16,
    protection_bits: int = 0,
    charge_fet: bool = True,
    discharge_fet: bool = True,
) -> bytes:
    """Inverse of :func:`decode_basic_info` (0.01 A units), for the simulator."""
    fet = (0x01 if charge_fet else 0) | (0x02 if discharge_fet else 0)
    date = ((24 - 0) << 9) | (6 << 5) | 1  # 2024-06-01
    head = struct.pack(
        ">HhHHHHHHH",
        int(round(voltage * 100)),
        int(round(current * 100)),
        int(round(remaining_ah * 100)),
        int(round(nominal_ah * 100)),
        cycles,
        date,
        0,
        0,
        protection_bits,
    )
    tail = bytes((0x21, max(0, min(100, soc)), fet, cell_count, len(temperatures)))
    temps = b"".join(struct.pack(">H", int(round(t * 10 + 2731))) for t in temperatures)
    return head + tail + temps
