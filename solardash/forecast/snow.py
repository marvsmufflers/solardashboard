"""Snow (or anything else) covering the panels.

Compares what the array actually produced with what it should have produced
for the sunlight Open-Meteo estimated at the cabin, hour by hour over the last
day or two. The weather station's own sensor is not used: it sheds snow on a
different schedule than the roof.

Hours are only counted when they can tell us something:

* enough sun that the array should make a real amount (``min_expected_kw``);
* the battery was not nearly full — a full battery makes the inverter throttle
  the panels back, which looks exactly like snow.

The result is a cover *factor* (actual / expected) with a state:

* ``clear``   factor ≥ 0.65 (normal day-to-day noise and partial shade)
* ``partial`` 0.3 – 0.65
* ``covered`` below 0.3

Once covered/partial, the state sticks until sunny hours show the array
producing again (factor ≥ ``clear_at``), so a string of grey days does not make
the app forget the panels are still under snow. While it sticks, the solar
forecast is multiplied by the factor, so the generator planner stops counting
on sun the panels cannot use.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

from ..storage.sqlite import HistoryStore

KV_COVER = "panel_cover"


@dataclass
class PanelCover:
    state: str = "clear"  # clear | partial | covered | unknown
    factor: float = 1.0  # multiply the solar forecast by this
    ratio: float | None = None  # latest measured actual / expected
    usable_hours: int = 0  # hours behind the latest measurement
    expected_kwh: float = 0.0
    actual_kwh: float = 0.0
    since: float | None = None  # when the current covered/partial spell began
    checked_at: float | None = None
    last_measured_at: float | None = None
    cleared_at: float | None = None  # "Panels cleared" pressed: older hours are ignored

    def to_dict(self) -> dict:
        return asdict(self)


def load_cover(store: HistoryStore) -> PanelCover:
    try:
        return PanelCover(**json.loads(store.get_kv(KV_COVER) or "{}"))
    except (TypeError, ValueError):
        return PanelCover()


def save_cover(store: HistoryStore, cover: PanelCover) -> None:
    store.set_kv(KV_COVER, json.dumps(cover.to_dict()))


def measure(store: HistoryStore, k_clear: float, now: float, lookback_h: float = 30.0, min_expected_kw: float = 0.4,
            full_soc: float = 95.0, since: float | None = None) -> tuple[float, float, int]:
    """(actual kWh, expected kWh, usable hours) over the lookback window."""
    start = now - lookback_h * 3600
    if since:
        start = max(start, (since // 3600 + 1) * 3600)  # whole hours after the panels were cleared
    end = (now // 3600) * 3600 - 1  # completed hours only
    ghi = dict(store.query("weather/ghi_forecast", start, end, step=3600))
    pv = dict(store.query("total/pv_power", start, end, step=3600)) or dict(store.query("inverter/pv_power", start, end, step=3600))
    soc = dict(store.query("total/battery_state_of_charge", start, end, step=3600))
    act = exp = 0.0
    n = 0
    for ts, g in ghi.items():
        if g is None or ts not in pv:
            continue
        expected_kw = k_clear * g / 1000.0
        if expected_kw < min_expected_kw:
            continue
        s = soc.get(ts)
        if s is not None and s >= full_soc:
            continue  # inverter may be throttling the panels: tells us nothing
        act += max(0.0, pv[ts]) / 1000.0
        exp += expected_kw
        n += 1
    return act, exp, n


def update_cover(prev: PanelCover, act: float, exp: float, n: int, now: float, min_hours: int = 2,
                 min_expected_kwh: float = 1.5, covered_below: float = 0.3, partial_below: float = 0.65,
                 clear_at: float = 0.7) -> PanelCover:
    c = PanelCover(**prev.to_dict())
    c.checked_at = now
    if n < min_hours or exp < min_expected_kwh:
        return c  # not enough sun to judge: keep what we knew
    ratio = act / exp
    c.ratio, c.usable_hours, c.expected_kwh, c.actual_kwh, c.last_measured_at = round(ratio, 3), n, round(exp, 2), round(act, 2), now
    was_cleanish = prev.state in ("clear", "unknown")
    if ratio < covered_below:
        state = "covered"
    elif ratio < partial_below:
        state = "partial"
    elif not was_cleanish and ratio < clear_at:
        state = "partial"  # hysteresis: was covered, improving, not clear yet
    else:
        state = "clear"
    if state == "clear":
        c.state, c.factor, c.since = "clear", 1.0, None
    else:
        if was_cleanish:
            c.since = now
        c.state, c.factor = state, round(max(0.05, min(1.0, ratio)), 3)
    return c


def check_cover(store: HistoryStore, k_clear: float, now: float | None = None, learned: bool = True) -> PanelCover:
    """``learned`` = the array's capacity comes from history. Until then only an unmistakable
    "covered" is reported, never "partial" (the configured size may simply be optimistic)."""
    now = now or time.time()
    prev = load_cover(store)
    act, exp, n = measure(store, k_clear, now, since=prev.cleared_at)
    cover = update_cover(prev, act, exp, n, now, partial_below=0.65 if learned else 0.3)
    save_cover(store, cover)
    return cover


def mark_cleared(store: HistoryStore, now: float | None = None) -> PanelCover:
    """Someone cleared the panels: back to clear now, and judge only from hours after this."""
    now = now or time.time()
    c = PanelCover(state="clear", factor=1.0, checked_at=now, cleared_at=now)
    save_cover(store, c)
    return c


def describe(cover: PanelCover) -> str | None:
    if cover.state not in ("covered", "partial") or cover.ratio is None:
        return None
    pct = round(cover.ratio * 100)
    what = "look snow-covered" if cover.state == "covered" else "look partly covered"
    return (f"Panels {what}: {pct} % of the solar the sky should give "
            f"({cover.actual_kwh:.1f} of {cover.expected_kwh:.1f} kWh over {cover.usable_hours} sunny hours). "
            "The forecast assumes they stay that way until they start producing again.")
