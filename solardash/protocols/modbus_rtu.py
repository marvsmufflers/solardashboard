"""Minimal Modbus RTU master (client) and frame helpers.

Only what the inverter and Chargeverter need: function codes 0x03, 0x04,
0x06 and 0x10. Pure Python, no pymodbus dependency.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from .crc import append_crc, check_crc
from .link import Link

READ_HOLDING = 0x03
READ_INPUT = 0x04
WRITE_SINGLE = 0x06
WRITE_MULTIPLE = 0x10

EXCEPTION_NAMES = {
    1: "Illegal function",
    2: "Illegal data address",
    3: "Illegal data value",
    4: "Slave device failure",
    5: "Acknowledge",
    6: "Slave device busy",
    8: "Memory parity error",
    10: "Gateway path unavailable",
    11: "Gateway target failed to respond",
}


class ModbusError(Exception):
    pass


class ModbusTimeout(ModbusError):
    pass


class ModbusCrcError(ModbusError):
    pass


class ModbusException(ModbusError):
    def __init__(self, function: int, code: int) -> None:
        self.function = function
        self.code = code
        super().__init__(f"Modbus exception {code} ({EXCEPTION_NAMES.get(code, 'unknown')}) for function 0x{function:02X}")


# ---------------------------------------------------------------------------
# frame builders / parsers (stateless; reused by the slave emulator)
# ---------------------------------------------------------------------------

def build_read_request(unit: int, function: int, address: int, count: int) -> bytes:
    return append_crc(struct.pack(">BBHH", unit, function, address, count))


def build_write_single(unit: int, address: int, value: int) -> bytes:
    return append_crc(struct.pack(">BBHH", unit, WRITE_SINGLE, address, value & 0xFFFF))


def build_write_multiple(unit: int, address: int, values: list[int]) -> bytes:
    body = struct.pack(">BBHHB", unit, WRITE_MULTIPLE, address, len(values), len(values) * 2)
    body += b"".join(struct.pack(">H", v & 0xFFFF) for v in values)
    return append_crc(body)


def build_read_response(unit: int, function: int, values: list[int]) -> bytes:
    body = struct.pack(">BBB", unit, function, len(values) * 2)
    body += b"".join(struct.pack(">H", v & 0xFFFF) for v in values)
    return append_crc(body)


def build_exception_response(unit: int, function: int, code: int) -> bytes:
    return append_crc(bytes((unit, function | 0x80, code)))


@dataclass
class ReadRequest:
    unit: int
    function: int
    address: int
    count: int


def parse_read_request(frame: bytes) -> ReadRequest | None:
    """Parse an 8-byte FC03/FC04 request, or return None if it is not one."""
    if len(frame) != 8 or not check_crc(frame):
        return None
    unit, fn, addr, count = struct.unpack(">BBHH", frame[:6])
    if fn not in (READ_HOLDING, READ_INPUT):
        return None
    return ReadRequest(unit, fn, addr, count)


def parse_read_response(frame: bytes, unit: int, function: int) -> list[int]:
    if len(frame) < 5:
        raise ModbusTimeout(f"short/no response ({len(frame)} bytes)")
    if not check_crc(frame):
        raise ModbusCrcError(f"bad CRC: {frame.hex(' ')}")
    if frame[0] != unit:
        raise ModbusError(f"unexpected unit {frame[0]} (wanted {unit})")
    if frame[1] == (function | 0x80):
        raise ModbusException(function, frame[2])
    if frame[1] != function:
        raise ModbusError(f"unexpected function 0x{frame[1]:02X}")
    n = frame[2]
    data = frame[3 : 3 + n]
    if len(data) != n or n % 2:
        raise ModbusError("byte count mismatch")
    return list(struct.unpack(f">{n // 2}H", data))


def expected_response_length(function: int, count: int) -> int:
    if function in (READ_HOLDING, READ_INPUT):
        return 5 + 2 * count
    if function in (WRITE_SINGLE, WRITE_MULTIPLE):
        return 8
    return 256


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------

class ModbusRtuClient:
    """Blocking Modbus RTU master over a :class:`Link`.

    One client per physical bus; several unit IDs can share it. Calls are
    serialised by the link's own lock.
    """

    def __init__(self, link: Link, timeout: float = 1.0, retries: int = 2, inter_frame_gap: float = 0.02) -> None:
        self.link = link
        self.timeout = timeout
        self.retries = retries
        self.inter_frame_gap = inter_frame_gap
        self._last_tx = 0.0
        self.stats = {"requests": 0, "timeouts": 0, "crc_errors": 0, "exceptions": 0}

    # -- low level -----------------------------------------------------------
    def _transact(self, request: bytes, expect_len: int) -> bytes:
        gap = self.inter_frame_gap - (time.monotonic() - self._last_tx)
        if gap > 0:
            time.sleep(gap)
        self.link.flush_input()
        self.link.write(request)
        self._last_tx = time.monotonic()
        self.stats["requests"] += 1
        resp = self.link.read(expect_len, self.timeout)
        # An exception response is 5 bytes; if we got exactly that and it looks
        # like one, return it as-is (parse_* raises ModbusException).
        return resp

    def _request(self, unit: int, function: int, request: bytes, expect_len: int) -> bytes:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self._transact(request, expect_len)
                if len(resp) == 0:
                    raise ModbusTimeout(f"no response from unit {unit} fn 0x{function:02X}")
                if len(resp) == 5 and resp[1] == (function | 0x80) and check_crc(resp):
                    self.stats["exceptions"] += 1
                    raise ModbusException(function, resp[2])
                if not check_crc(resp):
                    raise ModbusCrcError(f"bad CRC: {resp.hex(' ')}")
                return resp
            except ModbusException:
                raise
            except ModbusCrcError as e:
                self.stats["crc_errors"] += 1
                last = e
            except ModbusTimeout as e:
                self.stats["timeouts"] += 1
                last = e
            if attempt < self.retries:
                time.sleep(0.05 * (attempt + 1))
        assert last is not None
        raise last

    # -- public --------------------------------------------------------------
    def read_registers(self, unit: int, function: int, address: int, count: int) -> list[int]:
        if not 1 <= count <= 125:
            raise ValueError("count must be 1..125")
        req = build_read_request(unit, function, address, count)
        resp = self._request(unit, function, req, expected_response_length(function, count))
        return parse_read_response(resp, unit, function)

    def read_holding(self, unit: int, address: int, count: int) -> list[int]:
        return self.read_registers(unit, READ_HOLDING, address, count)

    def read_input(self, unit: int, address: int, count: int) -> list[int]:
        return self.read_registers(unit, READ_INPUT, address, count)

    def write_single(self, unit: int, address: int, value: int) -> None:
        req = build_write_single(unit, address, value)
        resp = self._request(unit, WRITE_SINGLE, req, 8)
        if resp[:6] != req[:6]:
            raise ModbusError(f"write echo mismatch: {resp.hex(' ')}")

    def write_multiple(self, unit: int, address: int, values: list[int]) -> None:
        req = build_write_multiple(unit, address, values)
        resp = self._request(unit, WRITE_MULTIPLE, req, 8)
        if resp[:2] != req[:2] or resp[2:4] != req[2:4]:
            raise ModbusError(f"write echo mismatch: {resp.hex(' ')}")


# ---------------------------------------------------------------------------
# passive frame splitter (for bus sniffing)
# ---------------------------------------------------------------------------

class RtuFrameSplitter:
    """Split a byte stream into Modbus RTU frames.

    Modbus RTU has no delimiters: frames are separated by silence. When bytes
    arrive in bursts (one ``read`` per burst) the burst *is* the frame. When
    bursts are glued together, we fall back to CRC scanning: a frame is
    accepted at the shortest prefix whose CRC checks out.
    """

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes, burst_boundary: bool = True) -> list[bytes]:
        self.buf += data
        frames: list[bytes] = []
        while True:
            frame = self._extract()
            if frame is None:
                break
            frames.append(frame)
        if burst_boundary and self.buf:
            # whatever is left is garbage (no valid CRC anywhere) – drop it,
            # unless it could be the start of a frame still in flight
            if len(self.buf) > 256:
                self.buf.clear()
        return frames

    def _extract(self) -> bytes | None:
        n = len(self.buf)
        if n < 4:
            return None
        # try the structurally-likely lengths first: 8 (request), then
        # 5+2k (read responses), then anything else
        candidates: list[int] = []
        if n >= 8:
            candidates.append(8)
        if n >= 5:
            cnt = self.buf[2]
            L = 5 + cnt
            if 5 <= L <= n and cnt % 2 == 0:
                candidates.append(L)
        candidates.extend(L for L in range(4, n + 1) if L not in candidates)
        for L in candidates:
            if check_crc(bytes(self.buf[:L])):
                frame = bytes(self.buf[:L])
                del self.buf[:L]
                return frame
        # no valid frame yet; if the buffer is huge, resync by dropping a byte
        if n > 300:
            del self.buf[0]
        return None
