import math
import time
from datetime import datetime, timezone

from solardash.forecast.model import HourForecast, daily_summary, fit_model, predict_hours
from solardash.forecast.planner import PlannerParams, parse_quiet_hours, plan_generator, simulate
from solardash.storage.sqlite import HistoryStore
from solardash.weather import solar
from solardash.weather.wunderground import WundergroundClient, parse_forecast, parse_observation

LAT, LON = 39.0, -105.5  # somewhere in the Colorado mountains

# --- solar geometry -----------------------------------------------------------

def test_solar_noon_and_night():
    # local solar noon on June 21 ~ 19:02 UTC at lon -105.5
    el_noon, az = solar.solar_position(LAT, LON, datetime(2026, 6, 21, 19, 2, tzinfo=timezone.utc))
    assert 73 < el_noon < 75  # 90 - 39 + 23.44 = 74.4
    assert 170 < az < 190
    el_mid, _ = solar.solar_position(LAT, LON, datetime(2026, 6, 21, 7, 0, tzinfo=timezone.utc))
    assert el_mid < 0
    el_dec, _ = solar.solar_position(LAT, LON, datetime(2026, 12, 21, 19, 0, tzinfo=timezone.utc))
    assert 26 < el_dec < 29  # 90 - 39 - 23.44 = 27.6


def test_clear_sky_curve():
    assert solar.clear_sky_ghi(0) == 0 and solar.clear_sky_ghi(-10) == 0
    assert 950 < solar.clear_sky_ghi(90) < 1100
    assert solar.clear_sky_ghi(30) < solar.clear_sky_ghi(60)
    prof = solar.clear_sky_hourly(LAT, LON, datetime(2026, 6, 21, 6, 0, tzinfo=timezone.utc), 24)
    assert len(prof) == 24
    day_wh = sum(g for _, g in prof)
    assert 7500 < day_wh < 9500  # clear June day ~8.5 kWh/m²
    assert solar.cloud_transmission(0) == 1.0 and math.isclose(solar.cloud_transmission(1), 0.25)
    rise, set_ = solar.sun_times(LAT, LON, datetime(2026, 6, 21, 12, tzinfo=timezone.utc))
    assert rise is not None and set_ is not None and 14 < (set_ - rise).total_seconds() / 3600 < 15.5


# --- wunderground parsing -----------------------------------------------------

OBS = {"observations": [{"stationID": "KCOTEST1", "obsTimeUtc": "2026-11-15T20:00:00Z", "epoch": 1794772800,
                         "lat": 39.0, "lon": -105.5, "solarRadiation": 412.3, "uv": 3.0, "winddir": 270, "humidity": 35,
                         "imperial": {"temp": 41.0, "heatIndex": 41, "dewpt": 16, "windChill": 36, "windSpeed": 5,
                                      "windGust": 12, "pressure": 29.92, "precipRate": 0.0, "precipTotal": 0.1, "elev": 8500}}]}
FC = {"validTimeLocal": ["2026-11-15T07:00:00-0700", "2026-11-16T07:00:00-0700"], "dayOfWeek": ["Sunday", "Monday"],
      "sunriseTimeLocal": ["2026-11-15T06:45:00-0700", "2026-11-16T06:46:00-0700"],
      "sunsetTimeLocal": ["2026-11-15T16:45:00-0700", "2026-11-16T16:44:00-0700"],
      "temperatureMax": [None, 50], "calendarDayTemperatureMax": [45, 50], "temperatureMin": [20, 22], "qpf": [0.0, 0.2], "qpfSnow": [0, 1.5],
      "narrative": ["Sunny.", "Snow showers."],
      "daypart": [{"daypartName": [None, "Sunday night", "Monday", "Monday night"], "cloudCover": [None, 10, 85, 60],
                   "precipChance": [None, 0, 70, 40], "wxPhraseLong": [None, "Clear", "Snow Showers", "Cloudy"],
                   "temperature": [None, 20, 50, 22], "windSpeed": [None, 5, 15, 10], "narrative": [None, "Clear.", "Snow.", "Cloudy."]}]}


def test_parse_observation_imperial_to_metric():
    o = parse_observation(OBS, "e")
    assert o.station_id == "KCOTEST1" and o.lat == 39.0
    assert abs(o.temperature_c - 5.0) < 0.01
    assert o.solar_radiation == 412.3 and o.uv == 3.0
    assert abs(o.wind_kph - 8.05) < 0.01 and abs(o.pressure_hpa - 1013.2) < 0.5
    assert abs(o.precip_total_mm - 2.54) < 0.01
    assert o.to_dict()["station_id"] == "KCOTEST1" and "raw" not in o.to_dict()


def test_parse_forecast_dayparts():
    days = parse_forecast(FC, "e")
    assert [d.date for d in days] == ["2026-11-15", "2026-11-16"]
    assert days[0].day is None and days[0].night.cloud_cover == 0.1
    assert days[0].cloud_for_hour(12) == 0.1  # falls back to the night part when today's day part is gone
    assert days[1].day.cloud_cover == 0.85 and days[1].cloud_for_hour(12) == 0.85 and days[1].cloud_for_hour(22) == 0.6
    assert days[1].day.precip_chance == 0.7 and abs(days[1].qpf_mm - 5.08) < 0.01
    assert abs(days[0].temp_max_c - 7.22) < 0.01  # calendar-day fallback
    assert days[1].to_dict()["day"]["phrase"] == "Snow Showers"


def test_client_builds_urls_and_counts_calls():
    seen = []

    def fetch(url):
        seen.append(url)
        return OBS if "observations/current" in url else FC

    c = WundergroundClient("KEY", "KCOTEST1", fetch=fetch)
    o = c.current()
    days = c.forecast(o.lat, o.lon)
    assert c.calls == 2 and len(days) == 2
    assert "stationId=KCOTEST1" in seen[0] and "apiKey=KEY" in seen[0]
    assert "geocode=39.0000%2C-105.5000" in seen[1] and "5day" in seen[1]


# --- model ----------------------------------------------------------------------

def _hours(start, n, pv_kw, load_kw):
    return [HourForecast(ts=start + 3600 * i, clear_sky_wm2=0, cloud=0.3, pv_kw=pv_kw(i), load_kw=load_kw) for i in range(n)]


def test_fit_model_without_history_uses_configured_size(tmp_path):
    st = HistoryStore(tmp_path / "h.db")
    m = fit_model(st, LAT, LON, pv_kw_peak=6.0)
    assert m.k_clear_source == "configured" and abs(m.k_clear - 4.8) < 0.01 and m.bias == 1.0
    assert m.load_profile_source == "default"


def test_fit_model_learns_from_history(tmp_path):
    st = HistoryStore(tmp_path / "h.db")
    now = int(time.time())
    # 20 days of a 5 kW array under a clear sky, 600 W constant load, station pyranometer
    prof = dict(solar.clear_sky_hourly(LAT, LON, datetime.fromtimestamp(now - 20 * 86400, timezone.utc), 20 * 24 + 1))
    for ts, cs in prof.items():
        st.insert(ts + 1800, {"total/pv_power": 5.0 * cs, "total/load_power": 600.0, "weather/solar_radiation": cs,
                              "forecast/pv_power_pred": 4.0 * cs})
    m = fit_model(st, LAT, LON, pv_kw_peak=6.0, now=now)
    assert m.k_clear_source == "learned" and 4.7 < m.k_clear < 5.3
    assert m.k_meas is not None and abs(m.k_meas - 0.005) < 0.0005
    assert 1.2 < m.bias < 1.3  # actual was 25 % above what we "predicted"
    assert m.load_profile_source == "learned" and all(abs(x - 0.6) < 0.01 for x in m.load_profile_kw)
    hours = predict_hours(m, LAT, LON, [], now, 48)
    assert len(hours) == 48 and max(h.pv_kw for h in hours) > 1.0
    days = daily_summary(hours)
    assert days and days[0]["pv_kwh"] >= 0


# --- planner -------------------------------------------------------------------

def _params(**kw):
    p = PlannerParams(battery_kwh=15.36, horizon_h=48)
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_no_generator_when_sunny():
    now = int(time.time() // 3600) * 3600
    hours = _hours(now, 49, lambda i: 3.0 if 8 <= (i % 24) <= 16 else 0.0, 0.5)
    plan = plan_generator(70.0, now, hours, _params())
    assert plan.needed is False and plan.recommended_now is False
    assert plan.min_soc_without > 28


def test_generator_planned_just_in_time_and_sized():
    now = int(time.time() // 3600) * 3600
    # cloudy: 0.3 kW midday only, 0.8 kW load -> steady drain
    hours = _hours(now, 49, lambda i: 0.3 if 10 <= (i % 24) <= 14 else 0.0, 0.8)
    plan = plan_generator(45.0, now, hours, _params())
    assert plan.needed is True
    assert plan.start_at is not None and plan.start_at >= now
    p = _params()
    # it should not start immediately: SOC 45 with a 25 % floor gives hours of headroom (just in time)
    assert plan.start_at > now + 3600
    assert plan.start_soc is not None and p.soc_floor <= plan.start_soc <= p.soc_floor + p.soc_margin + 2
    assert plan.min_soc_without < p.soc_floor
    assert min(s for t, s in plan.planned if t <= plan.end_at) >= p.soc_floor - 0.1  # never below the floor through the run
    assert plan.end_soc <= p.soc_ceiling and plan.run_h >= p.min_run_h - 0.01
    assert "Start" in plan.reason
    assert plan.next_deficit_at is not None and "another run" in plan.reason  # sustained deficit


def test_generator_start_now_when_already_low():
    now = int(time.time() // 3600) * 3600
    hours = _hours(now, 49, lambda i: 0.0, 1.0)
    plan = plan_generator(27.0, now, hours, _params())
    assert plan.needed and plan.recommended_now and plan.start_at == now
    assert plan.run_h <= 8.0


def test_quiet_hours_push_start_earlier():
    now = int(time.time() // 3600) * 3600
    hours = _hours(now, 49, lambda i: 0.0, 0.8)
    p = _params(quiet_hours=parse_quiet_hours(["22:00-07:00"]))
    plan = plan_generator(60.0, now, hours, p)
    assert plan.needed
    for t in range(int(plan.start_at), int(plan.end_at), 900):
        h = time.localtime(t).tm_hour
        assert not (h >= 22 or h < 7) or "Constraints" in plan.reason


def test_running_generator_gets_stop_advice():
    now = int(time.time() // 3600) * 3600
    hours = _hours(now, 49, lambda i: 2.0 if 9 <= (i % 24) <= 15 else 0.0, 0.6)
    plan = plan_generator(88.0, now, hours, _params(), generator_running=True)
    assert plan.stop_recommended is True or plan.end_soc <= 90
    plan2 = plan_generator(40.0, now, hours, _params(), generator_running=True)
    assert plan2.stop_recommended is False and plan2.recommended_now is True and plan2.end_soc > 40


def test_simulate_monotonic_with_generator():
    now = int(time.time() // 3600) * 3600
    hours = _hours(now, 49, lambda i: 0.0, 0.5)
    base = simulate(50.0, now, {h.ts: h for h in hours}, _params())
    withgen = simulate(50.0, now, {h.ts: h for h in hours}, _params(), gen_start=now, gen_end=now + 2 * 3600)
    assert withgen[8][1] > base[8][1]
