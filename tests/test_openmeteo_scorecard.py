import time
from datetime import datetime, timezone

from solardash.forecast.model import PvModel, predict_hours
from solardash.forecast.planner import PlannerParams, plan_generator
from solardash.forecast.model import HourForecast
from solardash.forecast.scorecard import scorecard
from solardash.storage.sqlite import HistoryStore
from solardash.weather.openmeteo import OpenMeteoClient, parse_forecast
from solardash.weather.solar import clear_sky_hourly

LAT, LON = 39.0, -105.5

OM = {"latitude": 39.0, "longitude": -105.5, "elevation": 2600.0,
      "hourly": {"time": ["2026-11-16T18:00", "2026-11-16T19:00", "2026-11-16T20:00"],
                 "shortwave_radiation": [350.0, 420.5, 300.0], "direct_radiation": [250, 300, 200],
                 "diffuse_radiation": [100, 120.5, 100], "cloud_cover": [20, 10, 40], "temperature_2m": [2.5, 3.0, 2.0],
                 "snowfall": [0, 0, 0.7], "precipitation": [0, 0, 1.2], "weather_code": [1, 0, 71]}}


def test_parse_openmeteo_shifts_to_hour_start():
    f = parse_forecast(OM)
    assert len(f.hours) == 3 and f.elevation_m == 2600.0
    first = f.hours[0]
    # "18:00" is the mean over 17:00-18:00 UTC -> keyed at 17:00
    assert first.ts == int(datetime(2026, 11, 16, 17, tzinfo=timezone.utc).timestamp())
    assert first.ghi == 350.0 and first.cloud == 0.2 and first.temperature_c == 2.5
    assert f.hours[2].snowfall_cm == 0.7 and f.hours[2].weather_code == 71
    assert f.by_ts()[first.ts] is first
    assert f.to_dict()["hours"] == 3


def test_openmeteo_client_url():
    seen = []
    c = OpenMeteoClient(fetch=lambda u: (seen.append(u), OM)[1])
    f = c.forecast(LAT, LON, forecast_days=7, past_days=2)
    assert c.calls == 1 and len(f.hours) == 3
    assert "latitude=39.0000" in seen[0] and "shortwave_radiation" in seen[0] and "past_days=2" in seen[0] and "timezone=UTC" in seen[0]


def test_predict_prefers_openmeteo_then_falls_back():
    model = PvModel(k_clear=5.0, k_clear_source="configured", k_meas=None, bias=1.0, samples_clear=0, samples_meas=0,
                    samples_bias=0, fitted_at=0, load_profile_kw=[0.4] * 24, load_profiles={"occupied": [1.2] * 24, "away": [0.2] * 24})
    start = int(time.time() // 3600) * 3600
    prof = clear_sky_hourly(LAT, LON, datetime.fromtimestamp(start, timezone.utc), 48)
    # Open-Meteo covers only the first 24 h, at 50 % of clear sky
    from solardash.weather.openmeteo import HourlyPoint
    om = {ts: HourlyPoint(ts=ts, ghi=cs * 0.5, cloud=0.5) for ts, cs in prof[:24]}
    hours = predict_hours(model, LAT, LON, [], start, 48, openmeteo=om, mode="occupied")
    assert hours[0].source == "openmeteo" and hours[30].source == "climatology"
    assert all(h.load_kw == 1.2 for h in hours)
    day_om = [h for h in hours[:24] if h.clear_sky_wm2 > 200]
    assert day_om and all(abs(h.pv_kw - 5.0 * h.ghi_wm2 / 1000.0) < 0.002 for h in day_om)  # values are rounded
    away = predict_hours(model, LAT, LON, [], start, 2, mode="away")
    assert away[0].load_kw == 0.2
    unknown = predict_hours(model, LAT, LON, [], start, 2, mode="party")
    assert unknown[0].load_kw == 0.4


def _hours(start, n, pv_kw, load_kw):
    return [HourForecast(ts=start + 3600 * i, clear_sky_wm2=0, cloud=0.3, pv_kw=pv_kw(i), load_kw=load_kw) for i in range(n)]


def test_planner_leaves_room_for_tomorrows_sun():
    """Sun tomorrow: charge only what is needed to get there, not up to the ceiling."""
    now = int(time.time() // 3600) * 3600
    p = PlannerParams(battery_kwh=15.36, horizon_h=72)
    sunny = _hours(now, 73, lambda i: 4.0 if 9 <= (i % 24) <= 15 else 0.0, 0.3)
    plan = plan_generator(30.0, now, sunny, p)
    assert plan.needed and plan.run_h >= p.min_run_h - 0.01
    assert plan.end_soc < 60.0  # the sun does the rest
    assert max(s for _, s in plan.planned) <= 100.0


def test_planner_one_long_run_before_a_dark_spell():
    """No sun for days: one long run (more charge) instead of several short ones, capped at the ceiling and the tank."""
    now = int(time.time() // 3600) * 3600
    p = PlannerParams(battery_kwh=15.36, horizon_h=72)
    dark = _hours(now, 73, lambda i: 0.1 if 10 <= (i % 24) <= 14 else 0.0, 0.25)
    plan = plan_generator(30.0, now, dark, p)
    assert plan.needed and plan.end_soc > 60.0 and plan.end_soc <= p.soc_ceiling + 0.01
    assert plan.run_h <= p.max_run_h + 0.01


def test_running_generator_respects_min_run():
    now = int(time.time() // 3600) * 3600
    p = PlannerParams(battery_kwh=15.36, horizon_h=72)
    sunny = _hours(now, 73, lambda i: 4.0 if 9 <= (i % 24) <= 15 else 0.0, 0.3)
    early = plan_generator(60.0, now, sunny, p, generator_running=True, run_elapsed_h=0.5)
    assert not early.stop_recommended  # only half an hour in: keep going until the engine is properly hot
    late = plan_generator(89.6, now, sunny, p, generator_running=True, run_elapsed_h=4.0)
    assert late.stop_recommended  # at the ceiling


def test_refill_warning_when_tank_is_short():
    now = int(time.time() // 3600) * 3600
    p = PlannerParams(battery_kwh=15.36, horizon_h=72, tank_left_h=0.5)
    dark = _hours(now, 73, lambda i: 0.0, 0.4)
    plan = plan_generator(30.0, now, dark, p)
    assert plan.needed and "Refill first" in plan.reason


def test_scorecard(tmp_path):
    st = HistoryStore(tmp_path / "s.db")
    now = int(time.time())
    day0 = now - (now % 86400) + 6 * 3600  # some time today (UTC-based is fine for the test)
    base = min(day0, now - 4 * 3600)
    # 2 h generator run at 2400 W, then 1 h at 1200 W (taper), bank at floor for 30 min
    for k in range(0, 48):
        t = base + k * 300
        cv = 2400.0 if k < 24 else (1200.0 if k < 36 else 0.0)
        soc = 28.0 if k < 6 else 40.0
        st.insert(t, {"total/chargeverter_power": cv, "total/battery_state_of_charge": soc, "total/pv_power": 0.0})
    rows = scorecard(st, LAT, LON, soc_floor=30.0, k_clear=5.0, days=3, now=now)
    today = rows[0]
    assert today["starts"] == 1 and abs(today["run_h"] - 3.0) < 0.01
    assert abs(today["kwh"] - (2.4 * 2 + 1.2 * 1)) < 0.05
    assert abs(today["full_current_share"] - 2 / 3) < 0.01
    assert abs(today["hours_at_floor"] - 0.5) < 0.01 and today["soc_min"] == 28.0


def test_min_run_never_pushes_past_ceiling():
    now = int(time.time() // 3600) * 3600
    p = PlannerParams(battery_kwh=15.36, horizon_h=72, generator_dc_kw=30.0, min_run_h=1.0)
    hours = _hours(now, 73, lambda i: 0.0, 0.8)
    plan = plan_generator(40.0, now, hours, p)
    assert plan.needed and plan.end_soc <= p.soc_ceiling + 0.01
