"""PV production model learned from history + load profile.

Prediction for an hour:

    pv_kw = k_clear × clearsky_kW/m² × cloud_transmission(cloud) × bias

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
    load_profile_kw: list[float] = field(default_factory=lambda: [0.5] * 24)  # by local hour
    load_profile_source: str = "default"
    load_days: int = 0

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
              days: int = 30, previous: PvModel | None = None) -> PvModel:
    now = now or time.time()
    start = now - days * 86400
    pv = dict(store.query("total/pv_power", start, now, step=3600))
    if not pv:
        pv = dict(store.query("inverter/pv_power", start, now, step=3600))
    irr = dict(store.query("weather/solar_radiation", start, now, step=3600))
    pred = dict(store.query("forecast/pv_power_pred", now - 7 * 86400, now, step=3600))

    clear = dict(clear_sky_hourly(lat, lon, datetime.fromtimestamp(start, timezone.utc), int(days * 24) + 1))

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

    # load profile by local hour, median across days
    load = store.query("total/load_power", now - 14 * 86400, now, step=3600)
    if not load:
        load = store.query("inverter/load_power", now - 14 * 86400, now, step=3600)
    by_hour: dict[int, list[float]] = {h: [] for h in range(24)}
    days_seen = set()
    for ts, w in load:
        lt = time.localtime(ts)
        by_hour[lt.tm_hour].append(w / 1000.0)
        days_seen.add((lt.tm_year, lt.tm_yday))
    if sum(len(v) for v in by_hour.values()) >= 24:
        overall = statistics.median([w for v in by_hour.values() for w in v])
        profile = [statistics.median(v) if v else overall for h, v in sorted(by_hour.items())]
        l_source = "learned"
    else:
        profile = (previous.load_profile_kw if previous else [0.5] * 24)
        l_source = previous.load_profile_source if previous else "default"
    return PvModel(k_clear=round(k_clear, 3), k_clear_source=k_source, k_meas=None if k_meas is None else round(k_meas, 5),
                   bias=round(bias, 3), samples_clear=len(ratios), samples_meas=len(meas), samples_bias=nb,
                   fitted_at=now, load_profile_kw=[round(x, 3) for x in profile], load_profile_source=l_source,
                   load_days=len(days_seen))


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

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def predict_hours(model: PvModel, lat: float, lon: float, forecast_days: list, start: float, hours: int,
                  default_cloud: float = 0.5) -> list[HourForecast]:
    """Hourly PV/load prediction from ``start`` for ``hours`` hours."""
    by_date = {d.date: d for d in forecast_days}
    out = []
    for ts, cs in clear_sky_hourly(lat, lon, datetime.fromtimestamp(start, timezone.utc), hours):
        lt = time.localtime(ts)
        day = by_date.get(time.strftime("%Y-%m-%d", lt))
        cloud = day.cloud_for_hour(lt.tm_hour) if day is not None else default_cloud
        pv_kw = model.k_clear * (cs / 1000.0) * cloud_transmission(cloud) * model.bias
        out.append(HourForecast(ts=ts, clear_sky_wm2=round(cs, 1), cloud=round(cloud, 2), pv_kw=round(max(0.0, pv_kw), 3),
                                load_kw=model.load_profile_kw[lt.tm_hour]))
    return out


def daily_summary(hours: list[HourForecast]) -> list[dict]:
    days: dict[str, dict[str, Any]] = {}
    for h in hours:
        d = time.strftime("%Y-%m-%d", time.localtime(h.ts))
        e = days.setdefault(d, {"date": d, "pv_kwh": 0.0, "load_kwh": 0.0, "cloud_sum": 0.0, "n": 0, "hours": 0})
        e["pv_kwh"] += h.pv_kw
        e["load_kwh"] += h.load_kw
        e["cloud_sum"] += h.cloud
        e["n"] += 1
    out = []
    for e in days.values():
        out.append({"date": e["date"], "pv_kwh": round(e["pv_kwh"], 1), "load_kwh": round(e["load_kwh"], 1),
                    "net_kwh": round(e["pv_kwh"] - e["load_kwh"], 1), "cloud_avg": round(e["cloud_sum"] / max(1, e["n"]), 2),
                    "hours_covered": e["n"]})
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
