"""Open-Meteo hourly forecast (free, no API key).

Gives what a PV forecast actually needs: hourly **global horizontal
irradiance** (``shortwave_radiation``, W/m² averaged over the preceding hour),
direct/diffuse split, cloud cover, temperature, snowfall and precipitation,
up to 16 days ahead, plus ``past_days`` of the same for calibration.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

BASE = "https://api.open-meteo.com/v1/forecast"
USER_AGENT = "solardash-weather/1.0"
HOURLY_VARS = ["shortwave_radiation", "direct_radiation", "diffuse_radiation", "cloud_cover", "temperature_2m",
               "snowfall", "precipitation", "weather_code"]


class OpenMeteoError(Exception):
    pass


def default_fetch(url: str, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


@dataclass
class HourlyPoint:
    ts: int  # hour start (epoch, UTC)
    ghi: float | None  # W/m² mean over the hour
    direct: float | None = None
    diffuse: float | None = None
    cloud: float | None = None  # 0..1
    temperature_c: float | None = None
    snowfall_cm: float | None = None
    precipitation_mm: float | None = None
    weather_code: int | None = None


@dataclass
class OpenMeteoForecast:
    fetched_at: float
    lat: float
    lon: float
    elevation_m: float | None
    hours: list[HourlyPoint] = field(default_factory=list)

    def by_ts(self) -> dict[int, HourlyPoint]:
        return {h.ts: h for h in self.hours}

    def to_dict(self) -> dict:
        return {"fetched_at": self.fetched_at, "lat": self.lat, "lon": self.lon, "elevation_m": self.elevation_m,
                "hours": len(self.hours), "first_ts": self.hours[0].ts if self.hours else None,
                "last_ts": self.hours[-1].ts if self.hours else None}


def _parse_time(s: str) -> int:
    # ISO "2026-11-15T13:00" in the requested timezone (we request UTC)
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def parse_forecast(data: dict) -> OpenMeteoForecast:
    if "hourly" not in data:
        raise OpenMeteoError(f"unexpected response: {str(data)[:200]}")
    h = data["hourly"]
    times = h.get("time") or []
    if not times:
        raise OpenMeteoError("empty hourly block")

    def col(name: str, i: int):
        arr = h.get(name) or []
        return arr[i] if i < len(arr) else None

    pts = []
    for i, t in enumerate(times):
        ts = _parse_time(t)
        # Open-Meteo radiation values are means over the *preceding* hour; shift to hour-start keys
        ts_start = ts - 3600
        cc = col("cloud_cover", i)
        pts.append(HourlyPoint(
            ts=ts_start, ghi=col("shortwave_radiation", i), direct=col("direct_radiation", i),
            diffuse=col("diffuse_radiation", i), cloud=None if cc is None else cc / 100.0,
            temperature_c=col("temperature_2m", i), snowfall_cm=col("snowfall", i),
            precipitation_mm=col("precipitation", i), weather_code=col("weather_code", i),
        ))
    return OpenMeteoForecast(fetched_at=time.time(), lat=float(data.get("latitude", 0)), lon=float(data.get("longitude", 0)),
                             elevation_m=data.get("elevation"), hours=pts)


class OpenMeteoClient:
    def __init__(self, fetch: Callable[[str], dict] | None = None) -> None:
        self.fetch = fetch or default_fetch
        self.calls = 0

    def forecast(self, lat: float, lon: float, forecast_days: int = 7, past_days: int = 2) -> OpenMeteoForecast:
        params = {
            "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "hourly": ",".join(HOURLY_VARS),
            "forecast_days": forecast_days, "past_days": past_days, "timezone": "UTC",
        }
        self.calls += 1
        return parse_forecast(self.fetch(f"{BASE}?{urllib.parse.urlencode(params)}"))
