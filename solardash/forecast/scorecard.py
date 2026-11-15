"""Daily scorecard: is the generator strategy working?

Computed from history samples per local day:

* generator starts, run hours, inferred kWh delivered, kWh per run hour;
* share of run time at full Chargeverter current (≥ 85 % of the best seen);
* hours the bank spent at or below the floor;
* curtailed solar: daylight hours with a full bank while the sky offered more
  than the array produced (the energy a generator run the night before
  displaced).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from ..storage.sqlite import HistoryStore
from ..weather.solar import clear_sky_hourly


def _day_bounds(day: datetime) -> tuple[int, int]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())


def scorecard(store: HistoryStore, lat: float | None, lon: float | None, soc_floor: float, k_clear: float,
              days: int = 14, step: int = 300, now: float | None = None) -> list[dict]:
    now = now or time.time()
    out = []
    today = datetime.fromtimestamp(now)
    for d in range(days):
        day = today - timedelta(days=d)
        t0, t1 = _day_bounds(day)
        t1 = min(t1, int(now))
        if t1 <= t0:
            continue
        cv = store.query("total/chargeverter_power", t0, t1, step)
        soc = store.query("total/battery_state_of_charge", t0, t1, step)
        pv = store.query("total/pv_power", t0, t1, step)
        if not cv and not soc:
            continue
        dt_h = step / 3600.0
        run_pts = [(t, p) for t, p in cv if p > 0]
        run_h = len(run_pts) * dt_h
        kwh = sum(p for _, p in run_pts) * dt_h / 1000.0
        peak = max((p for _, p in run_pts), default=0.0)
        full = sum(1 for _, p in run_pts if peak > 0 and p >= 0.85 * peak) * dt_h
        starts = 0
        prev = False
        for _, p in cv:
            on = p > 0
            if on and not prev:
                starts += 1
            prev = on
        floor_h = sum(1 for _, s in soc if s <= soc_floor) * dt_h
        soc_min = min((s for _, s in soc), default=None)
        soc_max = max((s for _, s in soc), default=None)
        curtailed_kwh = 0.0
        if lat is not None and lon is not None and pv and soc and k_clear:
            clear = dict(clear_sky_hourly(lat, lon, datetime.fromtimestamp(t0, timezone.utc), 25))
            soc_map = dict(soc)
            for t, p in pv:
                cs = clear.get(int(t // 3600) * 3600, 0.0)
                s = soc_map.get(t)
                if cs > 200 and s is not None and s >= 97.0:
                    potential = k_clear * cs  # W
                    if potential > p * 1.3:
                        curtailed_kwh += (potential - p) * dt_h / 1000.0
        out.append({
            "date": day.strftime("%Y-%m-%d"), "starts": starts, "run_h": round(run_h, 2), "kwh": round(kwh, 1),
            "kwh_per_run_h": round(kwh / run_h, 2) if run_h > 0 else None,
            "full_current_share": round(full / run_h, 2) if run_h > 0 else None,
            "hours_at_floor": round(floor_h, 2), "soc_min": soc_min, "soc_max": soc_max,
            "curtailed_kwh": round(curtailed_kwh, 1), "partial_day": d == 0,
        })
    return out
