"""PV production model learned from history + load profile.

Prediction for an hour (best available source first):

    pv_kw = k_clear × GHI_forecast_kW/m² × bias            (Open-Meteo hourly irradiance)
    pv_kw = k_clear × clearsky_kW/m² × cloud_transmission(cloud) × bias   (WU daypart cloud cover)
    pv_kw = k_clear × clearsky_kW/m² × cloud_transmission(0.5) × bias     (nothing else)

* ``k_clear`` – how many kW the array makes per kW/m² of clear-sky GHI. Learned
  as the 90th percentile of pv/clear-sky over recent daylight hours (that is,
  what the array does on clear hours), falling back to the configured array
  size until there is history.
* ``cloud_transmission`` – Kasten & Czeplak from the forecast cloud cover.
* ``bias`` – ratio of actual to predicted over the last week, so persistent
  errors (shading, snow on the panels, an optimistic forecast feed) wash out.

When the weather station has a solar sensor, ``k_meas`` (kW per W/m² measured)
is learned too, and "expected now" from live irradiance is exposed so the UI
can show when the array under-performs the sky.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..storage.sqlite import HistoryStore
from ..weather.solar import clear_sky_hourly, cloud_transmission

KV_MODEL = "pv_model"


@dataclass
class PvModel:
    k_clear: float  # kW per kW/m² clear-sky
    k_clear_source: str  # learned | configured
    k_meas: float | None  # kW per W/m² measured irradiance
    bias: float
    samples_clear: int
    samples_meas: int
    samples_bias: int
    fitted_at: float
    load_profile_kw: list[float] = field(default_factory=lambda: [0.5] * 24)  # by local hour (all data)
    load_profile_source: str = "default"
    load_days: int = 0
    load_profiles: dict[str, list[float]] = field(default_factory=dict)  # per occupancy mode
    load_profile_days: dict[str, int] = field(default_factory=dict)

    def profile_for(self, mode: str | None) -> list[float]:
        if mode and mode in self.load_profiles:
            return self.load_profiles[mode]
        return self.load_profile_kw

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        raise ValueError("empty")
    s = sorted(values)
    k = (len(s) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def fit_model(store: HistoryStore, lat: float, lon: float, pv_kw_peak: float, now: float | None = None,
              days: int = 30, previous: PvModel | None = None, modes: list[str] | None = None) -> PvModel:
    now = now or time.time()
    start = now - days * 86400
    pv = dict(store.query("total/pv_power", start, now, step=3600))
    if not pv:
        pv = dict(store.query("inverter/pv_power", start, now, step=3600))
    irr = dict(store.query("weather/solar_radiation", start, now, step=3600))
    pred = dict(store.query("forecast/pv_power_pred", now - 7 * 86400, now, step=3600))

    clear = dict(clear_sky_hourly(lat, lon, datetime.fromtimestamp(start, timezone.utc), int(days * 24) + 1))
    # hours when the panels were known to be snow-covered say nothing about the array's capacity
    covered = {ts for ts, f in store.query("forecast/panel_cover", start, now, step=3600) if f is not None and f < 0.9}
    pv = {ts: w for ts, w in pv.items() if ts not in covered}

    # k_clear from clear-ish hours
    ratios = []
    for ts, p_w in pv.items():
        cs = clear.get(ts)
        if cs and cs > 150 and p_w is not None:
            ratios.append((p_w / 1000.0) / (cs / 1000.0))
    if len(ratios) >= 20:
        k_clear = _percentile(ratios, 0.90)
        k_source = "learned"
        if pv_kw_peak:
            k_clear = max(0.3 * pv_kw_peak, min(1.3 * pv_kw_peak, k_clear))
    else:
        k_clear = (pv_kw_peak or 5.0) * 0.8
        k_source = "configured"

    # k_meas from the station's pyranometer
    meas = []
    for ts, p_w in pv.items():
        g = irr.get(ts)
        if g and g > 100 and p_w is not None and p_w > 0:
            meas.append((p_w / 1000.0) / g)
    k_meas = statistics.median(meas) if len(meas) >= 10 else None

    # bias: actual vs what we predicted for those hours
    num = den = 0.0
    nb = 0
    for ts, p_pred in pred.items():
        act = pv.get(ts)
        if act is None or p_pred is None or p_pred < 50:
            continue
        num += act
        den += p_pred
        nb += 1
    bias = max(0.4, min(1.6, num / den)) if nb >= 12 and den > 0 else 1.0

    # load profile by local hour, median across days (overall and per occupancy mode)
    load_days_back = 60 if modes else 14
    load = store.query("total/load_power", now - load_days_back * 86400, now, step=3600)
    if not load:
        load = store.query("inverter/load_power", now - load_days_back * 86400, now, step=3600)
    occ = dict(store.query("total/occupancy", now - load_days_back * 86400, now, step=3600)) if modes else {}

    def build(points: list[tuple[int, float]]) -> tuple[list[float] | None, int]:
        by_hour: dict[int, list[float]] = {h: [] for h in range(24)}
        seen = set()
        for ts, w in points:
            lt = time.localtime(ts)
            by_hour[lt.tm_hour].append(w / 1000.0)
            seen.add((lt.tm_year, lt.tm_yday))
        if sum(len(v) for v in by_hour.values()) < 24:
            return None, len(seen)
        overall = statistics.median([w for v in by_hour.values() for w in v])
        return [round(statistics.median(v) if v else overall, 3) for h, v in sorted(by_hour.items())], len(seen)

    recent = [(ts, w) for ts, w in load if ts >= now - 14 * 86400]
    profile, ndays = build(recent)
    if profile is not None:
        l_source = "learned"
    else:
        profile = (previous.load_profile_kw if previous else [0.5] * 24)
        l_source = previous.load_profile_source if previous else "default"
    profiles: dict[str, list[float]] = dict(previous.load_profiles) if previous else {}
    profile_days: dict[str, int] = dict(previous.load_profile_days) if previous else {}
    for idx, mode in enumerate(modes or []):
        pts = [(ts, w) for ts, w in load if round(occ.get(ts, -1)) == idx]
        prof, nd = build(pts)
        if prof is not None:
            profiles[mode] = prof
            profile_days[mode] = nd
    return PvModel(k_clear=round(k_clear, 3), k_clear_source=k_source, k_meas=None if k_meas is None else round(k_meas, 5),
                   bias=round(bias, 3), samples_clear=len(ratios), samples_meas=len(meas), samples_bias=nb,
                   fitted_at=now, load_profile_kw=[round(x, 3) for x in profile], load_profile_source=l_source,
                   load_days=ndays, load_profiles=profiles, load_profile_days=profile_days)


def save_model(store: HistoryStore, model: PvModel) -> None:
    store.set_kv(KV_MODEL, json.dumps(model.to_dict()))


def load_model(store: HistoryStore) -> PvModel | None:
    raw = store.get_kv(KV_MODEL)
    if not raw:
        return None
    try:
        return PvModel(**json.loads(raw))
    except (TypeError, ValueError):
        return None


@dataclass
class HourForecast:
    ts: int  # hour start, epoch
    clear_sky_wm2: float
    cloud: float
    pv_kw: float
    load_kw: float
    source: str = "climatology"  # openmeteo | wunderground | climatology
    ghi_wm2: float | None = None
    snowfall_cm: float | None = None
    temperature_c: float | None = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def predict_hours(model: PvModel, lat: float, lon: float, forecast_days: list, start: float, hours: int,
                  default_cloud: float = 0.5, openmeteo: dict | None = None, mode: str | None = None,
                  pv_scale: float = 1.0) -> list[HourForecast]:
    """Hourly PV/load prediction from ``start`` for ``hours`` hours.

    ``openmeteo`` maps hour-start epoch -> HourlyPoint (with ``ghi``); it wins
    when present, Wunderground daypart cloud cover is the fallback, then a
    climatological 50 % cloud. ``pv_scale`` derates the array (snow on the panels).
    """
    by_date = {d.date: d for d in forecast_days}
    profile = model.profile_for(mode)
    out = []
    for ts, cs in clear_sky_hourly(lat, lon, datetime.fromtimestamp(start, timezone.utc), hours):
        lt = time.localtime(ts)
        om = openmeteo.get(ts) if openmeteo else None
        day = by_date.get(time.strftime("%Y-%m-%d", lt))
        snow = temp = ghi = None
        if om is not None and om.ghi is not None:
            # cap at 1.1× clear sky to guard against model/clear-sky mismatch at low sun
            ghi = min(float(om.ghi), 1.1 * cs) if cs > 0 else 0.0
            cloud = om.cloud if om.cloud is not None else (1.0 - ghi / cs if cs > 50 else default_cloud)
            pv_kw = model.k_clear * (ghi / 1000.0) * model.bias
            source = "openmeteo"
            snow, temp = om.snowfall_cm, om.temperature_c
        elif day is not None:
            cloud = day.cloud_for_hour(lt.tm_hour)
            pv_kw = model.k_clear * (cs / 1000.0) * cloud_transmission(cloud) * model.bias
            source = "wunderground"
        else:
            cloud = default_cloud
            pv_kw = model.k_clear * (cs / 1000.0) * cloud_transmission(cloud) * model.bias
            source = "climatology"
        out.append(HourForecast(ts=ts, clear_sky_wm2=round(cs, 1), cloud=round(max(0.0, min(1.0, cloud)), 2),
                                pv_kw=round(max(0.0, pv_kw * pv_scale), 3), load_kw=profile[lt.tm_hour], source=source,
                                ghi_wm2=None if ghi is None else round(ghi, 1), snowfall_cm=snow, temperature_c=temp))
    return out


def daily_summary(hours: list[HourForecast]) -> list[dict]:
    days: dict[str, dict[str, Any]] = {}
    for h in hours:
        d = time.strftime("%Y-%m-%d", time.localtime(h.ts))
        e = days.setdefault(d, {"date": d, "pv_kwh": 0.0, "load_kwh": 0.0, "cloud_sum": 0.0, "n": 0, "snow": 0.0,
                                "tmin": None, "sources": set()})
        e["pv_kwh"] += h.pv_kw
        e["load_kwh"] += h.load_kw
        e["cloud_sum"] += h.cloud
        e["n"] += 1
        e["sources"].add(h.source)
        if h.snowfall_cm:
            e["snow"] += h.snowfall_cm
        if h.temperature_c is not None:
            e["tmin"] = h.temperature_c if e["tmin"] is None else min(e["tmin"], h.temperature_c)
    out = []
    for e in days.values():
        out.append({"date": e["date"], "pv_kwh": round(e["pv_kwh"], 1), "load_kwh": round(e["load_kwh"], 1),
                    "net_kwh": round(e["pv_kwh"] - e["load_kwh"], 1), "cloud_avg": round(e["cloud_sum"] / max(1, e["n"]), 2),
                    "hours_covered": e["n"], "snow_cm": round(e["snow"], 1), "temp_min_c": e["tmin"],
                    "source": "openmeteo" if "openmeteo" in e["sources"] else ("wunderground" if "wunderground" in e["sources"] else "climatology"),
                    "snow_risk": bool(e["snow"] >= 2.0)})
    return out


def expected_now_kw(model: PvModel, solar_radiation: float | None) -> float | None:
    if model.k_meas is None or solar_radiation is None:
        return None
    return round(model.k_meas * solar_radiation, 3)


def hour_floor(ts: float) -> int:
    return int(ts // 3600) * 3600


def tomorrow_midnight(ts: float) -> float:
    lt = datetime.fromtimestamp(ts)
    return (lt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()
