"""Byte-stream link abstraction.

A *link* is anything with ``write(bytes)``, ``read(n, timeout)`` and ``close()``.
The real implementation wraps a pyserial port; tests and the simulator use
in-memory links that implement the same three methods.
"""

from __future__ import annotations

import threading
import time
from typing import Protocol


class Link(Protocol):
    def write(self, data: bytes) -> None: ...

    def read(self, max_bytes: int, timeout: float) -> bytes:
        """Return up to ``max_bytes`` bytes, waiting at most ``timeout`` seconds
        for the *first* byte. May return fewer bytes than requested."""
        ...

    def flush_input(self) -> None: ...

    def close(self) -> None: ...


class SerialLink:
    """pyserial-backed link. Opens lazily; drops the port on I/O errors so the
    next call reopens it (survives USB re-enumeration)."""

    def __init__(
        self,
        port: str,
        baudrate: int,
        parity: str = "N",
        stopbits: int = 1,
        bytesize: int = 8,
        rs485_rts_toggle: bool = False,
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.parity = parity
        self.stopbits = stopbits
        self.bytesize = bytesize
        self.rs485_rts_toggle = rs485_rts_toggle
        self._ser = None
        self._lock = threading.Lock()

    # -- lifecycle -----------------------------------------------------------
    def _open(self):
        import serial  # imported lazily so the package imports without pyserial

        if self._ser is not None and self._ser.is_open:
            return self._ser
        ser = serial.serial_for_url(
            self.port,
            baudrate=self.baudrate,
            bytesize=self.bytesize,
            parity=self.parity,
            stopbits=self.stopbits,
            timeout=0.05,
            write_timeout=1.0,
            exclusive=True,
        )
        if self.rs485_rts_toggle:
            try:
                ser.rs485_mode = serial.rs485.RS485Settings()
            except Exception:  # pragma: no cover - adapter specific
                pass
        self._ser = ser
        return ser

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                finally:
                    self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    # -- io ------------------------------------------------------------------
    def write(self, data: bytes) -> None:
        with self._lock:
            try:
                ser = self._open()
                ser.write(data)
                ser.flush()
            except Exception:
                self._drop()
                raise

    def read(self, max_bytes: int, timeout: float) -> bytes:
        with self._lock:
            try:
                ser = self._open()
                deadline = time.monotonic() + timeout
                buf = bytearray()
                # wait for first byte up to `timeout`, then drain what has arrived
                while len(buf) < max_bytes:
                    chunk = ser.read(max_bytes - len(buf))
                    if chunk:
                        buf += chunk
                        # after first data, allow a short inter-byte gap only
                        deadline = min(deadline, time.monotonic() + 0.08)
                    if time.monotonic() >= deadline:
                        break
                return bytes(buf)
            except Exception:
                self._drop()
                raise

    def flush_input(self) -> None:
        with self._lock:
            try:
                ser = self._open()
                ser.reset_input_buffer()
            except Exception:
                self._drop()
                raise

    def _drop(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None


class LoopbackLink:
    """In-memory link for tests: whatever a *responder* callable returns for a
    written request is queued as the next read."""

    def __init__(self, responder=None) -> None:
        self.responder = responder
        self.writes: list[bytes] = []
        self._rx = bytearray()

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))
        if self.responder is not None:
            resp = self.responder(bytes(data))
            if resp:
                self._rx += resp

    def feed(self, data: bytes) -> None:
        self._rx += data

    def read(self, max_bytes: int, timeout: float) -> bytes:
        out = bytes(self._rx[:max_bytes])
        del self._rx[:max_bytes]
        return out

    def flush_input(self) -> None:
        self._rx.clear()

    def close(self) -> None:
        pass
