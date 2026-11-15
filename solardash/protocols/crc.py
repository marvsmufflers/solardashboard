"""CRC-16/MODBUS (poly 0xA001 reflected, init 0xFFFF)."""

from __future__ import annotations

_TABLE: list[int] = []


def _build_table() -> None:
    for i in range(256):
        crc = i
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
        _TABLE.append(crc)


_build_table()


def crc16_modbus(data: bytes | bytearray | memoryview) -> int:
    """Return the CRC-16/MODBUS of ``data`` as an int (0..0xFFFF)."""
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _TABLE[(crc ^ b) & 0xFF]
    return crc


def append_crc(frame: bytes) -> bytes:
    """Append the CRC to a Modbus RTU frame (little-endian, as on the wire)."""
    crc = crc16_modbus(frame)
    return frame + bytes((crc & 0xFF, crc >> 8))


def check_crc(frame: bytes) -> bool:
    """True when the last two bytes of ``frame`` are a valid CRC of the rest."""
    if len(frame) < 3:
        return False
    expected = crc16_modbus(frame[:-2])
    return frame[-2] == (expected & 0xFF) and frame[-1] == (expected >> 8)
