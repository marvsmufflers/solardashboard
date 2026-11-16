"""Shared system state and a tiny async pub/sub bus."""

from __future__ import annotations

import asyncio
import copy
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DeviceHealth:
    name: str
    kind: str  # inverter | battery | chargeverter
    port: str
    connected: bool = False
    last_ok: float | None = None
    last_error: str | None = None
    error_count: int = 0
    poll_count: int = 0
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "port": self.port,
            "connected": self.connected,
            "seconds_since_ok": None if self.last_ok is None else round(time.time() - self.last_ok, 1),
            "last_error": self.last_error,
            "error_count": self.error_count,
            "poll_count": self.poll_count,
            "stats": self.stats,
        }


class SystemState:
    """Latest snapshot of everything. Mutated by pollers, read by API/MQTT."""

    def __init__(self) -> None:
        self.started_at = time.time()
        self.inverter: dict[str, Any] = {}
        self.inverter_settings: dict[str, Any] = {}
        self.inverter_holding_raw: dict[int, int] = {}
        self.inverter_updated: float | None = None
        self.settings_updated: float | None = None
        self.batteries: dict[int, dict[str, Any]] = {}  # index (1-based) -> data
        self.battery_updated: dict[int, float] = {}
        self.chargeverter: dict[str, Any] = {}
        self.derived: dict[str, Any] = {}
        self.weather: dict[str, Any] = {}
        self.forecast: dict[str, Any] = {}
        self.devices: dict[str, DeviceHealth] = {}
        self._lock = asyncio.Lock()
        self.bus = EventBus()

    def snapshot(self) -> dict:
        now = time.time()
        return {
            "ts": now,
            "uptime_s": round(now - self.started_at),
            "inverter": copy.deepcopy(self.inverter),
            "inverter_age_s": None if self.inverter_updated is None else round(now - self.inverter_updated, 1),
            "settings": copy.deepcopy(self.inverter_settings),
            "batteries": [
                dict(copy.deepcopy(b), index=i, age_s=round(now - self.battery_updated.get(i, now), 1))
                for i, b in sorted(self.batteries.items())
            ],
            "chargeverter": copy.deepcopy(self.chargeverter),
            "total": copy.deepcopy(self.derived),
            "weather": copy.deepcopy(self.weather),
            "forecast_summary": {k: self.forecast.get(k) for k in ("available", "updated", "days", "plan", "expected_now_kw", "actual_now_kw", "soc_now", "params", "reason")}
            if self.forecast else {},
            "devices": {k: v.to_dict() for k, v in self.devices.items()},
        }


class EventBus:
    """Fan-out of state-change notifications to async subscribers."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self, maxsize: int = 50) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, topic: str, payload: Any = None) -> None:
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait((topic, payload))
