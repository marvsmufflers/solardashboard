import time

from solardash.forecast.snow import PanelCover, check_cover, describe, load_cover, update_cover
from solardash.storage.sqlite import HistoryStore


def _day(store, now, pv_frac, soc=50.0, k_clear=5.0, hours=range(9, 16)):
    """Seed a sunny day: 600 W/m² forecast sun 9-15 h, array producing pv_frac of what it should."""
    base = int(now // 86400) * 86400 - 86400
    for h in hours:
        ts = base + h * 3600
        for m in range(0, 3600, 300):
            store.insert(ts + m, {"total/pv_power": pv_frac * k_clear * 0.6 * 1000, "total/battery_state_of_charge": soc})
        store.insert(ts, {"weather/ghi_forecast": 600.0})
    return base + 20 * 3600


def test_covered_panels_detected(tmp_path):
    st = HistoryStore(tmp_path / "a.db")
    now = _day(st, time.time(), pv_frac=0.08)
    c = check_cover(st, 5.0, now)
    assert c.state == "covered" and c.factor < 0.15 and c.usable_hours >= 5
    assert "snow-covered" in describe(c)


def test_full_battery_is_not_mistaken_for_snow(tmp_path):
    st = HistoryStore(tmp_path / "b.db")
    now = _day(st, time.time(), pv_frac=0.1, soc=99.0)  # inverter throttling: low output, battery full
    c = check_cover(st, 5.0, now)
    assert c.state == "clear" and c.factor == 1.0 and c.ratio is None


def test_clear_panels_and_partial(tmp_path):
    st = HistoryStore(tmp_path / "c.db")
    assert check_cover(st, 5.0, _day(st, time.time(), pv_frac=0.95)).state == "clear"
    st2 = HistoryStore(tmp_path / "d.db")
    c = check_cover(st2, 5.0, _day(st2, time.time(), pv_frac=0.5))
    assert c.state == "partial" and 0.4 < c.factor < 0.6


def test_cover_sticks_through_grey_days_and_clears_with_sun():
    now = time.time()
    c = update_cover(PanelCover(), act=0.5, exp=10.0, n=6, now=now)
    assert c.state == "covered" and c.since == now
    grey = update_cover(c, act=0.1, exp=0.5, n=1, now=now + 86400)  # not enough sun to judge
    assert grey.state == "covered" and grey.factor == c.factor
    melting = update_cover(grey, act=6.6, exp=10.0, n=6, now=now + 2 * 86400)  # 66 %: better, not clear yet
    assert melting.state == "partial"
    cleared = update_cover(melting, act=9.0, exp=10.0, n=6, now=now + 3 * 86400)
    assert cleared.state == "clear" and cleared.factor == 1.0 and cleared.since is None


def test_forecast_is_derated_while_covered():
    from solardash.forecast.model import PvModel, predict_hours

    m = PvModel(k_clear=5.0, k_clear_source="learned", k_meas=None, bias=1.0, samples_clear=0, samples_meas=0,
                samples_bias=0, fitted_at=0)
    start = int(time.time() // 86400) * 86400
    full = predict_hours(m, 45.0, 0.0, [], start, 24)
    snowy = predict_hours(m, 45.0, 0.0, [], start, 24, pv_scale=0.1)
    assert sum(h.pv_kw for h in full) > 0
    assert abs(sum(h.pv_kw for h in snowy) - 0.1 * sum(h.pv_kw for h in full)) < 0.05


def test_state_persists(tmp_path):
    st = HistoryStore(tmp_path / "e.db")
    now = _day(st, time.time(), pv_frac=0.05)
    check_cover(st, 5.0, now)
    assert load_cover(st).state == "covered"


def test_panels_cleared_resets_and_ignores_older_hours(tmp_path):
    from solardash.forecast.snow import mark_cleared

    st = HistoryStore(tmp_path / "f.db")
    now = _day(st, time.time(), pv_frac=0.05)
    assert check_cover(st, 5.0, now).state == "covered"
    mark_cleared(st, now)
    c = check_cover(st, 5.0, now + 600)  # the snowy hours are before the clearing: not counted
    assert c.state == "clear" and c.factor == 1.0


def test_unlearned_array_only_flags_clear_cut_cover(tmp_path):
    st = HistoryStore(tmp_path / "g.db")
    c = check_cover(st, 5.0, _day(st, time.time(), pv_frac=0.5), learned=False)
    assert c.state == "clear"  # 50 %: could just be an optimistic array size
    st2 = HistoryStore(tmp_path / "h.db")
    assert check_cover(st2, 5.0, _day(st2, time.time(), pv_frac=0.05), learned=False).state == "covered"
