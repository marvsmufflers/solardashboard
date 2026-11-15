"""Common polling loop with reconnect/backoff. Blocking serial I/O runs in a
worker thread via ``asyncio.to_thread`` so one slow bus never stalls others."""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod

from ..core.state import DeviceHealth, SystemState

log = logging.getLogger(__name__)


class Poller(ABC):
    def __init__(self, name: str, kind: str, port: str, state: SystemState, interval: float) -> None:
        self.name = name
        self.kind = kind
        self.port = port
        self.state = state
        self.interval = interval
        self.health = DeviceHealth(name=name, kind=kind, port=port)
        state.devices[f"{kind}:{name}"] = self.health
        self._stop = asyncio.Event()
        self._backoff = interval

    @abstractmethod
    def poll_once(self) -> None:
        """Blocking: talk to the device and update ``self.state``."""

    def on_disconnect(self) -> None:  # pragma: no cover - overridden where needed
        pass

    async def run(self) -> None:
        log.info("%s: starting poller on %s", self.name, self.port)
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                await asyncio.to_thread(self.poll_once)
                was_down = not self.health.connected
                self.health.connected = True
                self.health.last_ok = time.time()
                self.health.last_error = None
                self.health.poll_count += 1
                self._backoff = self.interval
                if was_down:
                    log.info("%s: connected", self.name)
                    self.state.bus.publish("device", self.health.to_dict())
            except Exception as e:  # noqa: BLE001 - any device error is a soft failure
                self.health.error_count += 1
                self.health.last_error = f"{type(e).__name__}: {e}"
                if self.health.connected or self.health.error_count in (1, 5, 20) or self.health.error_count % 100 == 0:
                    log.warning("%s: %s", self.name, self.health.last_error)
                if self.health.connected:
                    self.health.connected = False
                    self.on_disconnect()
                    self.state.bus.publish("device", self.health.to_dict())
                self._backoff = min(self._backoff * 1.5, 60.0)
            delay = (self._backoff if not self.health.connected else self.interval) - (time.monotonic() - t0)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.2, delay))
            except asyncio.TimeoutError:
                pass
        log.info("%s: poller stopped", self.name)

    def stop(self) -> None:
        self._stop.set()
