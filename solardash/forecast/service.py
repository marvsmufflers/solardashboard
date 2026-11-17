"""Weather polling and forecast/plan recomputation tasks."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Callable

from ..config import Config
from ..core.state import DeviceHealth, SystemState
from ..storage.sqlite import HistoryStore
from ..weather.openmeteo import OpenMeteoClient, OpenMeteoForecast
from ..weather.wunderground import DayForecast, WeatherError, WundergroundClient
from .model import (
    PvModel,
    daily_summary,
    expected_now_kw,
    fit_model,
    hour_floor,
    load_model,
    predict_hours,
    save_model,
)
from .planner import PlannerParams, parse_quiet_hours, plan_generator
from .scorecard import scorecard

log = logging.getLogger(__name__)


class WeatherTask:
    def __init__(self, cfg: Config, state: SystemState, store: HistoryStore, fetch: Callable[[str], dict] | None = None) -> None:
        self.cfg = cfg
        self.state = state
        self.store = store
        w = cfg.weather
        self.client = WundergroundClient(w.api_key, w.station_id, units=w.units, fetch=fetch)
        self.health = DeviceHealth(name="Weather Underground", kind="weather", port=w.station_id or "?")
        state.devices["weather:wunderground"] = self.health
        self.forecast: list[DayForecast] = []
        self._stop = asyncio.Event()
        self._forecast_due = 0.0
        self.state.weather = {"current": None, "forecast": [], "lat": w.latitude, "lon": w.longitude,
                              "station_id": w.station_id, "error": None}

    @property
    def location(self) -> tuple[float, float] | None:
        lat, lon = self.state.weather.get("lat"), self.state.weather.get("lon")
        if lat is None or lon is None:
            return None
        return float(lat), float(lon)

    def _poll_current(self) -> None:
        obs = self.client.current()
        w = self.state.weather
        w["current"] = obs.to_dict()
        w["current_updated"] = time.time()
        if w.get("lat") is None and obs.lat is not None:
            w["lat"], w["lon"] = obs.lat, obs.lon
        sample = {f"weather/{k}": v for k, v in (
            ("temperature", obs.temperature_c), ("humidity", obs.humidity), ("wind_kph", obs.wind_kph),
            ("pressure_hpa", obs.pressure_hpa), ("solar_radiation", obs.solar_radiation), ("uv", obs.uv),
            ("precip_rate_mm_h", obs.precip_rate_mm_h),
        ) if v is not None}
        if sample:
            self.store.insert(int(obs.ts), sample)

    def _poll_forecast(self) -> None:
        loc = self.location
        if loc is None:
            raise WeatherError("no latitude/longitude yet (set weather.latitude/longitude or wait for an observation)")
        self.forecast = self.client.forecast(*loc)
        self.state.weather["forecast"] = [d.to_dict() for d in self.forecast]
        self.state.weather["forecast_updated"] = time.time()

    async def run(self) -> None:
        log.info("weather: polling station %s every %.0fs", self.cfg.weather.station_id, self.cfg.weather.poll_interval)
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._poll_current)
                if time.monotonic() >= self._forecast_due:
                    await asyncio.to_thread(self._poll_forecast)
                    self._forecast_due = time.monotonic() + self.cfg.weather.forecast_interval
                self.health.connected = True
                self.health.last_ok = time.time()
                self.health.last_error = None
                self.health.poll_count += 1
                self.state.weather["error"] = None
                self.state.bus.publish("weather", None)
            except Exception as e:  # noqa: BLE001
                self.health.error_count += 1
                self.health.last_error = f"{type(e).__name__}: {e}"
                self.health.connected = False
                self.state.weather["error"] = self.health.last_error
                log.warning("weather: %s", self.health.last_error)
            self.health.stats = {"api_calls": self.client.calls}
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.weather.poll_interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


class ForecastTask:
    """Fits the PV model, predicts PV/load, runs the planner. Works without
    weather data too (clear-sky × default cloud), just less accurately."""

    def __init__(self, cfg: Config, state: SystemState, store: HistoryStore, weather: WeatherTask | None,
                 openmeteo_fetch: Callable[[str], dict] | None = None, occupancy_mode: Callable[[], str | None] | None = None) -> None:
        self.cfg = cfg
        self.state = state
        self.store = store
        self.weather = weather
        self.model: PvModel | None = load_model(store)
        self._stop = asyncio.Event()
        self._last_fit = 0.0
        self.openmeteo_client = OpenMeteoClient(fetch=openmeteo_fetch)
        self.openmeteo: OpenMeteoForecast | None = None
        self._openmeteo_due = 0.0
        self.openmeteo_error: str | None = None
        self.occupancy_mode = occupancy_mode or (lambda: None)
        self.health = DeviceHealth(name="Open-Meteo", kind="weather", port="api.open-meteo.com")
        if cfg.weather.openmeteo_enabled:
            state.devices["weather:openmeteo"] = self.health
        self.state.forecast = {"available": False, "reason": "not computed yet"}

    def _location(self) -> tuple[float, float] | None:
        w = self.cfg.weather
        if w.latitude is not None and w.longitude is not None:
            return float(w.latitude), float(w.longitude)
        if self.weather is not None:
            return self.weather.location
        return None

    def _battery_kwh(self) -> float:
        if self.cfg.planner.battery_kwh:
            return float(self.cfg.planner.battery_kwh)
        ah = self.state.derived.get("battery_capacity_ah") or self.state.inverter.get("battery_capacity_ah") or 300.0
        return round(ah * 51.2 / 1000.0, 2)

    def refresh_openmeteo(self) -> None:
        loc = self._location()
        if loc is None or not self.cfg.weather.openmeteo_enabled:
            return
        try:
            self.openmeteo = self.openmeteo_client.forecast(loc[0], loc[1], forecast_days=self.cfg.weather.openmeteo_days, past_days=2)
            self.openmeteo_error = None
            self.health.connected = True
            self.health.last_ok = time.time()
            self.health.last_error = None
            self.health.poll_count += 1
            # log the forecast GHI so the model can be checked against it later
            rows = {h.ts: h.ghi for h in self.openmeteo.hours if h.ghi is not None}
            for ts, g in rows.items():
                self.store.insert(ts, {"weather/ghi_forecast": g})
        except Exception as e:  # noqa: BLE001
            self.openmeteo_error = f"{type(e).__name__}: {e}"
            self.health.connected = False
            self.health.error_count += 1
            self.health.last_error = self.openmeteo_error
            log.warning("open-meteo: %s", self.openmeteo_error)
        self.health.stats = {"api_calls": self.openmeteo_client.calls}

    def params(self) -> PlannerParams:
        pc = self.cfg.planner
        return PlannerParams(
            soc_floor=pc.soc_floor, soc_target=pc.soc_target, soc_target_storm=pc.soc_target_storm,
            storm_lookahead_h=pc.storm_lookahead_h, soc_margin=pc.soc_margin,
            generator_dc_kw=pc.generator_dc_kw, charge_efficiency=pc.charge_efficiency, min_run_h=pc.min_run_h,
            max_run_h=pc.max_run_h, quiet_hours=parse_quiet_hours(pc.quiet_hours), horizon_h=pc.horizon_h,
            battery_kwh=self._battery_kwh(),
        )

    def recompute(self, now: float | None = None) -> dict:
        now = now or time.time()
        loc = self._location()
        if loc is None:
            self.state.forecast = {"available": False, "reason": "Predictions need the cabin's location. Open System, Setup, Location and weather, enter latitude and longitude, and save."}
            return self.state.forecast
        lat, lon = loc
        if time.monotonic() >= self._openmeteo_due:
            self.refresh_openmeteo()
            self._openmeteo_due = time.monotonic() + self.cfg.weather.openmeteo_interval
        modes = self.cfg.occupancy.modes if self.cfg.occupancy.enabled else None
        if self.model is None or now - self._last_fit > 3600:
            self.model = fit_model(self.store, lat, lon, self.cfg.planner.pv_kw_peak, now=now, previous=self.model, modes=modes)
            save_model(self.store, self.model)
            self._last_fit = now
        days = self.weather.forecast if self.weather is not None else []
        mode = self.occupancy_mode()
        hours = predict_hours(self.model, lat, lon, days, hour_floor(now), self.cfg.planner.horizon_h + 1,
                              openmeteo=self.openmeteo.by_ts() if self.openmeteo else None, mode=mode)
        # log predictions for later bias evaluation (next 48 h only)
        for h in hours[:48]:
            self.store.insert(h.ts, {"forecast/pv_power_pred": h.pv_kw * 1000.0})
        soc = self.state.derived.get("battery_state_of_charge")
        if soc is None:
            soc = self.state.inverter.get("battery_soc")
        plan = None
        if self.cfg.planner.enabled and soc is not None:
            running = any(g.get("running") for g in self.state.derived.get("generators", []))
            plan = plan_generator(float(soc), now, hours, self.params(), generator_running=running)
        obs = (self.state.weather or {}).get("current") if self.weather is not None else None
        exp_now = expected_now_kw(self.model, obs.get("solar_radiation") if obs else None)
        summary = daily_summary(hours)
        self.state.forecast = {
            "available": True,
            "updated": now,
            "location": {"lat": lat, "lon": lon},
            "model": self.model.to_dict(),
            "params": self.params().__dict__,
            "hours": [h.to_dict() for h in hours],
            "days": summary,
            "plan": plan.to_dict() if plan else None,
            "soc_now": soc,
            "expected_now_kw": exp_now,
            "actual_now_kw": round((self.state.inverter.get("pv_power") or 0) / 1000.0, 3) if self.state.inverter else None,
            "weather_days": len(days),
            "occupancy": mode,
            "sources": {
                "openmeteo": self.openmeteo.to_dict() if self.openmeteo else None,
                "openmeteo_error": self.openmeteo_error,
                "wunderground_days": len(days),
                "hours_by_source": {s: sum(1 for h in hours if h.source == s) for s in ("openmeteo", "wunderground", "climatology")},
            },
        }
        self.state.bus.publish("forecast", None)
        return self.state.forecast

    async def run(self) -> None:
        await asyncio.sleep(1)
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.recompute)
            except Exception as e:  # noqa: BLE001
                log.exception("forecast: %s", e)
                self.state.forecast = {"available": False, "reason": f"{type(e).__name__}: {e}"}
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.planner.recompute_interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


    def scorecard(self, days: int = 14) -> list[dict]:
        loc = self._location()
        return scorecard(self.store, loc[0] if loc else None, loc[1] if loc else None, self.cfg.planner.soc_floor,
                         self.model.k_clear if self.model else 0.0, days=days)


def plan_mqtt_values(forecast: dict[str, Any]) -> dict[str, Any]:
    """Flatten the forecast/plan into scalar MQTT values."""
    out: dict[str, Any] = {}
    if not forecast.get("available"):
        return out
    days = forecast.get("days") or []
    if days:
        out["forecast/pv_today_kwh"] = days[0]["pv_kwh"]
        out["forecast/load_today_kwh"] = days[0]["load_kwh"]
    if len(days) > 1:
        out["forecast/pv_tomorrow_kwh"] = days[1]["pv_kwh"]
    if forecast.get("expected_now_kw") is not None:
        out["forecast/pv_expected_now_kw"] = forecast["expected_now_kw"]
    plan = forecast.get("plan")
    if plan:
        for k in ("needed", "recommended_now", "stop_recommended", "run_h", "energy_kwh", "end_soc", "start_soc",
                  "min_soc_without", "reason"):
            if plan.get(k) is not None:
                out[f"plan/{k}"] = plan[k]
        for k in ("start_at", "end_at", "min_soc_at"):
            if plan.get(k):
                out[f"plan/{k}"] = datetime.fromtimestamp(plan[k]).astimezone().isoformat(timespec="seconds")
    return out
