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
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
from ..config import (
    OVERRIDABLE_SECTIONS,
    SECRET_FIELDS,
    Config,
    build_section,
    load_overrides,
    overrides_path,
    save_overrides,
    section_schema,
    section_values,
)
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


class SetupWrite(BaseModel):
    section: str
    values: Any


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

    app = FastAPI(title="Watts the Forecast", version=__version__, lifespan=lifespan)
    app.state.service = svc
    gate = install_password_gate(app, cfg.web.password) if cfg.web.password else None
    if gate:
        log.info("web: password protection enabled")

    # -- pages ---------------------------------------------------------------
    def _asset_version() -> str:
        import hashlib

        h = hashlib.sha1()
        for name in ("app.js", "style.css", "index.html", "manifest.json", "favicon.ico", "icon-180.png"):
            h.update((STATIC / name).read_bytes())
        return h.hexdigest()[:10]

    asset_version = _asset_version()

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return FileResponse(STATIC / "favicon.ico", media_type="image/x-icon")

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    @app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
    async def touch_icon():
        return FileResponse(STATIC / "icon-180.png", media_type="image/png")

    @app.get("/", include_in_schema=False)
    async def index():
        # version the asset URLs so browsers and Cloudflare never serve a stale UI after an update
        html = (STATIC / "index.html").read_text()
        for name in ("style.css", "app.js", "chart.umd.min.js", "manifest.json", "favicon.ico", "icon-180.png", "icon-64.png"):
            html = html.replace(f"/static/{name}", f"/static/{name}?v={asset_version}")
        return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    # -- api -----------------------------------------------------------------
    @app.get("/healthz")
    async def healthz():
        inv_ok = svc.inverter is None or svc.inverter.health.connected
        return {"ok": True, "inverter_connected": inv_ok, "version": __version__}

    @app.get("/api/state")
    async def api_state():
        snap = svc.state.snapshot()
        snap["display"] = asdict(cfg.display)
        snap["inverter"].pop("_raw", None)
        snap["sky"] = _sky(svc)
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
        if svc.forecast is not None:  # refresh the plan's wording for the new selection
            asyncio.get_running_loop().run_in_executor(None, svc.forecast.recompute)
        return {"active": name}

    @app.put("/api/generators/{name}/signal")
    async def api_generator_signal(name: str, body: GeneratorSignal):
        """For Home Assistant: PUT {"running": true, "contactor": false}."""
        try:
            return await asyncio.to_thread(svc.set_generator_signal, name, body.running, body.contactor)
        except KeyError as e:
            raise HTTPException(404, str(e))

    @app.post("/api/generators/{name}/{what}")
    async def api_generator_service(name: str, what: str):
        """Record a refill (what=refilled) or an oil change (what=oil_changed)."""
        if what not in ("refilled", "oil_changed"):
            raise HTTPException(404, "use refilled or oil_changed")
        try:
            return await asyncio.to_thread(svc.generator_service, name, what)
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

    # -- setup (app configuration from the UI) ------------------------------------
    @app.get("/api/setup")
    async def api_setup():
        ov = load_overrides(overrides_path(cfg))
        return {
            "overrides_path": str(overrides_path(cfg) or ""),
            "sections": {s: {"values": section_values(cfg, s), "schema": section_schema(s), "overridden": sorted((ov.get(s) or {}).keys()) if s != "generators" else bool(ov.get(s))}
                         for s in OVERRIDABLE_SECTIONS},
        }

    @app.get("/api/setup/ports")
    async def api_setup_ports():
        """Serial ports visible inside the container, and which config entry uses each."""
        def scan():
            try:
                from serial.tools import list_ports

                found = [{"device": p.device, "description": p.description, "hwid": p.hwid, "serial": p.serial_number}
                         for p in sorted(list_ports.comports(), key=lambda x: x.device)]
            except Exception as e:  # noqa: BLE001
                found = [{"error": str(e)}]
            import glob

            for dev in sorted(set(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/serial/by-id/*"))):
                if not any(f.get("device") == dev for f in found):
                    found.append({"device": dev, "description": "", "hwid": "", "serial": None})
            return found
        ports = await asyncio.to_thread(scan)
        used = {cfg.inverter.port: cfg.inverter.name}
        for b in cfg.batteries:
            if b.enabled:
                used[b.port] = b.name
        if cfg.chargeverter.enabled and cfg.chargeverter.mode != "off":
            used[cfg.chargeverter.port] = cfg.chargeverter.name
        for p in ports:
            p["used_by"] = used.get(p.get("device"))
        return {"ports": ports, "simulate": cfg.simulate}

    @app.put("/api/setup")
    async def api_setup_write(body: SetupWrite):
        if body.section not in OVERRIDABLE_SECTIONS:
            raise HTTPException(404, f"unknown section {body.section}")
        values = body.values
        if body.section != "generators":
            if not isinstance(values, dict):
                raise HTTPException(400, "values must be an object")
            # keep existing secrets when the form sends the mask or nothing
            current = section_values(cfg, body.section, mask_secrets=False)
            for (sec, key) in SECRET_FIELDS:
                if sec == body.section and values.get(key) in ("", "***", None):
                    values[key] = current.get(key, "")
            values = {k: v for k, v in values.items() if k in current}
        try:
            if body.section == "generators":
                built = build_section("generators", values)
            else:
                built = build_section(body.section, {**section_values(cfg, body.section, mask_secrets=False), **values})
        except (ValueError, TypeError) as e:
            raise HTTPException(400, f"invalid {body.section}: {e}")
        ov = load_overrides(overrides_path(cfg))
        if body.section == "generators":
            ov["generators"] = values
            cfg.generators = built
        else:
            ov[body.section] = {**(ov.get(body.section) or {}), **values}
            setattr(cfg, body.section, built)
        save_overrides(overrides_path(cfg), ov)
        applied = await svc.reconfigure(body.section)
        svc.store.add_event("info", "setup", f"{body.section} updated ({applied})")
        return {"section": body.section, "applied": applied, "values": section_values(cfg, body.section)}

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
    snap["display"] = asdict(svc.cfg.display)
    snap["inverter"].pop("_raw", None)
    snap["sky"] = _sky(svc)
    return snap


def _sky(svc: Service) -> dict | None:
    try:
        return svc.forecast.sky_now() if svc.forecast is not None else None
    except Exception:  # noqa: BLE001
        return None


def run(cfg: Config) -> None:
    import uvicorn

    app = create_app(cfg)
    uvicorn.run(app, host=cfg.web.host, port=cfg.web.port, log_level=cfg.log_level.lower(), access_log=False)
