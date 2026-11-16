"""FastAPI application: REST + WebSocket + static dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
from ..config import Config
from .auth import install_password_gate, websocket_allowed
from ..core.service import Service
from ..protocols import luxpower as lx
from ..protocols.luxpower import SettingValueError

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"

RANGES = {"1h": 3600, "3h": 3 * 3600, "6h": 6 * 3600, "12h": 12 * 3600, "24h": 86400, "48h": 2 * 86400,
          "7d": 7 * 86400, "30d": 30 * 86400, "90d": 90 * 86400, "1y": 365 * 86400}


class GeneratorSelect(BaseModel):
    name: str


class OccupancySet(BaseModel):
    mode: str


class GeneratorSignal(BaseModel):
    running: bool | None = None
    contactor: bool | None = None


class SettingWrite(BaseModel):
    value: Any
    confirm: bool = False


def sanitized_config(cfg: Config) -> dict:
    d = asdict(cfg)
    if d.get("mqtt"):
        d["mqtt"]["password"] = "***" if d["mqtt"].get("password") else ""
    if d.get("web"):
        d["web"]["password"] = "***" if d["web"].get("password") else ""
    if d.get("weather"):
        d["weather"]["api_key"] = "***" if d["weather"].get("api_key") else ""
    return d


def create_app(cfg: Config, service: Service | None = None) -> FastAPI:
    svc = service or Service(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await svc.start()
        try:
            yield
        finally:
            await svc.stop()

    app = FastAPI(title="solardash", version=__version__, lifespan=lifespan)
    app.state.service = svc
    gate = install_password_gate(app, cfg.web.password) if cfg.web.password else None
    if gate:
        log.info("web: password protection enabled")

    # -- pages ---------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    # -- api -----------------------------------------------------------------
    @app.get("/healthz")
    async def healthz():
        inv_ok = svc.inverter is None or svc.inverter.health.connected
        return {"ok": True, "inverter_connected": inv_ok, "version": __version__}

    @app.get("/api/state")
    async def api_state():
        snap = svc.state.snapshot()
        snap["inverter"].pop("_raw", None)
        return snap

    @app.get("/api/inverter/raw")
    async def api_inverter_raw():
        return {"input": svc.state.inverter.get("_raw", {}), "holding": svc.state.inverter_holding_raw}

    @app.get("/api/devices")
    async def api_devices():
        return {k: v.to_dict() for k, v in svc.state.devices.items()}

    @app.get("/api/config")
    async def api_config():
        return sanitized_config(cfg)

    @app.get("/api/events")
    async def api_events(limit: int = 100):
        return svc.store.events(limit)

    @app.get("/api/chargeverter")
    async def api_chargeverter():
        return {"state": svc.state.chargeverter, "derived": {
            k: svc.state.derived.get(k) for k in ("chargeverter_power", "chargeverter_power_raw", "chargeverter_power_method")
        }}

    @app.get("/api/generators")
    async def api_generators():
        return await asyncio.to_thread(svc.generator_summary)

    @app.put("/api/generators/active")
    async def api_generator_select(body: GeneratorSelect):
        try:
            name = await asyncio.to_thread(svc.set_active_generator, body.name)
        except KeyError as e:
            raise HTTPException(404, str(e))
        svc._track_generators()
        svc.state.bus.publish("total", None)
        return {"active": name}

    @app.put("/api/generators/{name}/signal")
    async def api_generator_signal(name: str, body: GeneratorSignal):
        """For Home Assistant: PUT {"running": true, "contactor": false}."""
        try:
            return await asyncio.to_thread(svc.set_generator_signal, name, body.running, body.contactor)
        except KeyError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/weather")
    async def api_weather():
        return svc.state.weather or {"enabled": False}

    @app.get("/api/forecast")
    async def api_forecast():
        return svc.state.forecast or {"available": False, "reason": "forecast task not running"}

    @app.post("/api/forecast/recompute")
    async def api_forecast_recompute():
        if svc.forecast is None:
            raise HTTPException(503, "forecast task not running")
        return await asyncio.to_thread(svc.forecast.recompute)

    @app.get("/api/occupancy")
    async def api_occupancy():
        return {"mode": svc.occupancy_mode(), "modes": cfg.occupancy.modes, "enabled": cfg.occupancy.enabled}

    @app.put("/api/occupancy")
    async def api_occupancy_set(body: OccupancySet):
        try:
            return {"mode": await asyncio.to_thread(svc.set_occupancy, body.mode)}
        except KeyError as e:
            raise HTTPException(404, str(e))

    @app.get("/api/scorecard")
    async def api_scorecard(days: int = 14):
        if svc.forecast is None:
            raise HTTPException(503, "forecast task not running")
        return await asyncio.to_thread(svc.forecast.scorecard, days)

    @app.get("/api/history/metrics")
    async def api_history_metrics():
        return svc.store.metrics()

    @app.get("/api/history")
    async def api_history(
        metrics: str = Query(..., description="comma-separated metric names, e.g. total/pv_power,total/load_power"),
        range: str | None = Query(None, description="1h,3h,6h,12h,24h,48h,7d,30d,90d,1y"),
        start: float | None = None,
        end: float | None = None,
        step: int | None = Query(None, description="bucket seconds; default picks ~400 points"),
    ):
        now = time.time()
        if range:
            if range not in RANGES:
                raise HTTPException(400, f"unknown range {range}")
            start, end = now - RANGES[range], now
        if start is None:
            start = now - 86400
        if end is None:
            end = now
        if step is None:
            step = max(int((end - start) / 400), int(cfg.history.sample_interval))
        names = [m.strip() for m in metrics.split(",") if m.strip()]
        data = await asyncio.to_thread(svc.store.query_many, names, start, end, step)
        return {"start": start, "end": end, "step": step, "series": data}

    @app.get("/api/energy/daily")
    async def api_energy_daily(days: int = 30):
        return svc.store.daily_energy(days)

    @app.get("/api/history/stats")
    async def api_history_stats():
        return svc.store.stats()

    # -- settings --------------------------------------------------------------
    @app.get("/api/settings")
    async def api_settings():
        cur = svc.state.inverter_settings
        out = []
        for s in lx.SETTINGS:
            val = cur.get(s.key)
            out.append({
                "key": s.key, "label": s.label, "group": s.group, "kind": s.kind, "register": s.reg,
                "value": val, "value_label": lx.setting_label(s, val), "unit": s.unit, "min": s.minimum,
                "max": s.maximum, "step": s.step, "options": list(s.options), "labels": list(s.labels),
                "writable": s.writable and cfg.inverter.allow_writes, "danger": s.danger,
            })
        return {"updated": svc.state.settings_updated, "allow_writes": cfg.inverter.allow_writes,
                "extra": {k: v for k, v in cur.items() if k not in lx.SETTINGS_BY_KEY}, "settings": out}

    @app.post("/api/settings/refresh")
    async def api_settings_refresh():
        try:
            return await svc.refresh_settings()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, str(e))

    @app.put("/api/settings/{key}")
    async def api_setting_write(key: str, body: SettingWrite):
        s = lx.SETTINGS_BY_KEY.get(key)
        if s is None:
            raise HTTPException(404, f"unknown setting {key}")
        if not cfg.inverter.allow_writes:
            raise HTTPException(403, "writes disabled: set inverter.allow_writes: true in config.yaml")
        if s.danger and not body.confirm:
            raise HTTPException(428, f"confirmation required: {s.danger}")
        try:
            return await svc.write_setting(key, body.value)
        except SettingValueError as e:
            raise HTTPException(400, str(e))
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("setting write failed")
            raise HTTPException(502, f"write failed: {e}")

    # -- websocket -----------------------------------------------------------------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        if not websocket_allowed(gate, ws.cookies, ws.headers):
            await ws.close(code=4401)
            return
        await ws.accept()
        q = svc.state.bus.subscribe()
        try:
            await ws.send_text(json.dumps(_ws_payload(svc)))
            last = time.monotonic()
            while True:
                try:
                    await asyncio.wait_for(q.get(), timeout=5.0)
                    # coalesce bursts: at most one push per second
                    while not q.empty():
                        q.get_nowait()
                    wait = 1.0 - (time.monotonic() - last)
                    if wait > 0:
                        await asyncio.sleep(wait)
                except asyncio.TimeoutError:
                    pass
                await ws.send_text(json.dumps(_ws_payload(svc)))
                last = time.monotonic()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            svc.state.bus.unsubscribe(q)

    return app


def _ws_payload(svc: Service) -> dict:
    snap = svc.state.snapshot()
    snap["inverter"].pop("_raw", None)
    return snap


def run(cfg: Config) -> None:
    import uvicorn

    app = create_app(cfg)
    uvicorn.run(app, host=cfg.web.host, port=cfg.web.port, log_level=cfg.log_level.lower(), access_log=False)
