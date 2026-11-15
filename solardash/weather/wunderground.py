"""Weather Underground (The Weather Company) client for PWS owners.

A PWS owner's free API key allows:

* ``/v2/pws/observations/current`` – the station's latest observation, which
  includes ``solarRadiation`` (W/m²) and ``uv`` when the station has the sensor;
* ``/v2/pws/history/hourly`` – the station's past days;
* ``/v3/wx/forecast/daily/5day`` – 5-day forecast with day/night "dayparts"
  carrying ``cloudCover`` (%), ``precipChance`` and narratives.

The JSON field names below follow TWC's published schemas; the parser is
tolerant of missing fields. ``solardash probe weather`` dumps the raw
responses so they can be checked against a real account.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

BASE = "https://api.weather.com"
USER_AGENT = "solardash-weather/1.0"


class WeatherError(Exception):
    pass


def default_fetch(url: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - https only
        if resp.status == 204:
            return {}
        return json.loads(resp.read().decode("utf-8"))


@dataclass
class Observation:
    ts: float
    station_id: str
    lat: float | None
    lon: float | None
    temperature_c: float | None = None
    humidity: float | None = None
    dewpoint_c: float | None = None
    wind_kph: float | None = None
    wind_gust_kph: float | None = None
    wind_dir: float | None = None
    pressure_hpa: float | None = None
    precip_rate_mm_h: float | None = None
    precip_total_mm: float | None = None
    solar_radiation: float | None = None  # W/m²
    uv: float | None = None
    raw: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "raw"}
        d["age_s"] = round(time.time() - self.ts)
        return d


@dataclass
class DayPart:
    name: str
    cloud_cover: float | None  # 0..1
    precip_chance: float | None  # 0..1
    phrase: str = ""
    temperature_c: float | None = None
    wind_kph: float | None = None
    narrative: str = ""


@dataclass
class DayForecast:
    date: str  # YYYY-MM-DD local
    valid_local: str
    sunrise_local: str | None
    sunset_local: str | None
    temp_max_c: float | None
    temp_min_c: float | None
    qpf_mm: float | None
    qpf_snow_cm: float | None
    narrative: str
    day: DayPart | None
    night: DayPart | None

    def cloud_for_hour(self, hour: int) -> float:
        """Cloud fraction for a local hour, using the day part 7-19h."""
        part = self.day if 7 <= hour < 19 else self.night
        if part is None or part.cloud_cover is None:
            other = self.night if part is self.day else self.day
            if other is not None and other.cloud_cover is not None:
                return other.cloud_cover
            return 0.5
        return part.cloud_cover

    def to_dict(self) -> dict:
        def dp(p: DayPart | None):
            return None if p is None else p.__dict__
        return {**{k: v for k, v in self.__dict__.items() if k not in ("day", "night")}, "day": dp(self.day), "night": dp(self.night)}


def _f(x) -> float | None:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _to_c(v: float | None, units: str) -> float | None:
    if v is None:
        return None
    return (v - 32.0) * 5.0 / 9.0 if units == "e" else v


def parse_observation(data: dict, units: str) -> Observation:
    obs_list = data.get("observations") or []
    if not obs_list:
        raise WeatherError("no observations in response (station offline or wrong station id)")
    o = obs_list[0]
    u = o.get("imperial" if units == "e" else "metric", {}) or {}
    ts = o.get("epoch")
    if ts is None:
        ts = time.time()
    wind_mult = 1.609344 if units == "e" else 1.0
    precip_mult = 25.4 if units == "e" else 1.0
    pressure = _f(u.get("pressure"))
    if pressure is not None and units == "e":
        pressure *= 33.8639
    return Observation(
        ts=float(ts),
        station_id=o.get("stationID", ""),
        lat=_f(o.get("lat")),
        lon=_f(o.get("lon")),
        temperature_c=_to_c(_f(u.get("temp")), units),
        humidity=_f(o.get("humidity")),
        dewpoint_c=_to_c(_f(u.get("dewpt")), units),
        wind_kph=None if _f(u.get("windSpeed")) is None else _f(u.get("windSpeed")) * wind_mult,
        wind_gust_kph=None if _f(u.get("windGust")) is None else _f(u.get("windGust")) * wind_mult,
        wind_dir=_f(o.get("winddir")),
        pressure_hpa=pressure,
        precip_rate_mm_h=None if _f(u.get("precipRate")) is None else _f(u.get("precipRate")) * precip_mult,
        precip_total_mm=None if _f(u.get("precipTotal")) is None else _f(u.get("precipTotal")) * precip_mult,
        solar_radiation=_f(o.get("solarRadiation")),
        uv=_f(o.get("uv")),
        raw=o,
    )


def parse_forecast(data: dict, units: str) -> list[DayForecast]:
    n = len(data.get("validTimeLocal") or [])
    if n == 0:
        raise WeatherError("empty forecast response")

    def col(name: str, i: int):
        arr = data.get(name) or []
        return arr[i] if i < len(arr) else None

    parts = (data.get("daypart") or [{}])[0] or {}

    def part(i: int) -> DayPart | None:
        def pc(name: str):
            arr = parts.get(name) or []
            return arr[i] if i < len(arr) else None
        if pc("daypartName") is None and pc("cloudCover") is None:
            return None
        cc = _f(pc("cloudCover"))
        pr = _f(pc("precipChance"))
        ws = _f(pc("windSpeed"))
        return DayPart(
            name=pc("daypartName") or "",
            cloud_cover=None if cc is None else cc / 100.0,
            precip_chance=None if pr is None else pr / 100.0,
            phrase=pc("wxPhraseLong") or pc("wxPhraseShort") or "",
            temperature_c=_to_c(_f(pc("temperature")), units),
            wind_kph=None if ws is None else ws * (1.609344 if units == "e" else 1.0),
            narrative=pc("narrative") or "",
        )

    out = []
    for i in range(n):
        valid = col("validTimeLocal", i) or ""
        qpf = _f(col("qpf", i))
        snow = _f(col("qpfSnow", i))
        out.append(DayForecast(
            date=valid[:10],
            valid_local=valid,
            sunrise_local=col("sunriseTimeLocal", i),
            sunset_local=col("sunsetTimeLocal", i),
            temp_max_c=_to_c(_f(col("temperatureMax", i)) if col("temperatureMax", i) is not None else _f(col("calendarDayTemperatureMax", i)), units),
            temp_min_c=_to_c(_f(col("temperatureMin", i)), units),
            qpf_mm=None if qpf is None else qpf * (25.4 if units == "e" else 1.0),
            qpf_snow_cm=None if snow is None else snow * (2.54 if units == "e" else 1.0),
            narrative=col("narrative", i) or "",
            day=part(2 * i),
            night=part(2 * i + 1),
        ))
    return out


class WundergroundClient:
    def __init__(self, api_key: str, station_id: str, units: str = "e", fetch: Callable[[str], dict] | None = None) -> None:
        if units not in ("e", "m"):
            raise ValueError("units must be 'e' or 'm'")
        self.api_key = api_key
        self.station_id = station_id
        self.units = units
        self.fetch = fetch or default_fetch
        self.calls = 0

    def _url(self, path: str, **params: Any) -> str:
        params = {"format": "json", "units": self.units, "apiKey": self.api_key, **params}
        return f"{BASE}{path}?{urllib.parse.urlencode(params)}"

    def current(self) -> Observation:
        self.calls += 1
        data = self.fetch(self._url("/v2/pws/observations/current", stationId=self.station_id, numericPrecision="decimal"))
        return parse_observation(data, self.units)

    def forecast(self, lat: float, lon: float) -> list[DayForecast]:
        self.calls += 1
        data = self.fetch(self._url("/v3/wx/forecast/daily/5day", geocode=f"{lat:.4f},{lon:.4f}", language="en-US"))
        return parse_forecast(data, self.units)

    def history_hourly(self, date: datetime) -> list[Observation]:
        """One day of hourly station history (for backfilling solar radiation)."""
        self.calls += 1
        data = self.fetch(self._url("/v2/pws/history/hourly", stationId=self.station_id, date=date.strftime("%Y%m%d"), numericPrecision="decimal"))
        out = []
        for o in data.get("observations") or []:
            try:
                out.append(parse_observation({"observations": [o]}, self.units))
            except WeatherError:
                continue
        return out
