"""End-to-end with simulated devices through the FastAPI app."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from solardash.api.app import create_app
from solardash.config import BatteryConfig, Config
from solardash.core.service import Service


@pytest.fixture
def cfg(tmp_path):
    c = Config(simulate=True)
    c.batteries = [BatteryConfig(name=f"B{i}", port=f"sim{i}", poll_interval=0.2) for i in (1, 2, 3)]
    c.inverter.poll_interval = 0.2
    c.inverter.settings_poll_interval = 1.0
    c.inverter.allow_writes = True
    c.chargeverter.enabled = True
    c.chargeverter.mode = "emulate"
    c.history.path = str(tmp_path / "t.db")
    c.history.sample_interval = 0.3
    c.weather.enabled = True
    c.weather.api_key = "sim"
    c.weather.station_id = "KSIM0001"
    c.weather.poll_interval = 0.5
    return c


async def _wait_for(pred, timeout=5.0):
    t0 = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(0.1)
    return False


async def test_full_stack(cfg):
    svc = Service(cfg)
    app = create_app(cfg, svc)
    async with app.router.lifespan_context(app):
        assert await _wait_for(lambda: svc.state.inverter and len(svc.state.batteries) == 3 and svc.state.derived.get("battery_count") == 3)
        assert await _wait_for(lambda: svc.state.chargeverter.get("poll_count", 0) >= 1 and svc.chargeverter.answered >= 1, timeout=6)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/state")
            assert r.status_code == 200
            d = r.json()
            assert d["inverter"]["status"] and d["total"]["battery_source"] == "bms"
            assert len(d["batteries"]) == 3 and d["batteries"][0]["cell_count"] == 16
            assert d["chargeverter"]["mode"] == "emulate" and d["chargeverter"]["presented_soc"] is not None
            r = await c.get("/api/settings")
            s = r.json()
            assert s["allow_writes"] is True
            keys = {x["key"]: x for x in s["settings"]}
            assert keys["eps_output_voltage"]["value"] == 240
            assert keys["generator_charge_stop_capacity"]["value"] == 90
            # write a safe numeric setting and verify
            r = await c.put("/api/settings/generator_charge_stop_capacity", json={"value": 85})
            assert r.status_code == 200, r.text
            assert r.json()["generator_charge_stop_capacity"] == 85
            # dangerous setting requires confirm
            r = await c.put("/api/settings/eps_output_voltage", json={"value": 230})
            assert r.status_code == 428
            r = await c.put("/api/settings/eps_output_voltage", json={"value": 230, "confirm": True})
            assert r.status_code == 200
            # out of range
            r = await c.put("/api/settings/generator_charge_stop_capacity", json={"value": 5})
            assert r.status_code == 400
            # bit setting with read-modify-write
            r = await c.put("/api/settings/ac_charge", json={"value": True})
            assert r.status_code == 200 and r.json()["ac_charge"] is True
            await asyncio.sleep(0.8)
            r = await c.get("/api/history?metrics=total/pv_power,battery_1/soc&range=1h")
            series = r.json()["series"]
            assert series["total/pv_power"] and series["battery_1/soc"]
            r = await c.get("/api/devices")
            assert all(v["connected"] for k, v in r.json().items() if not k.startswith("weather:"))
            r = await c.get("/api/chargeverter")
            assert r.json()["state"]["charger_online"] is True
            r = await c.get("/api/generators")
            g = r.json()
            assert g["active"] == "Outhouse generator (auto-start)" and len(g["generators"]) == 2
            r = await c.put("/api/generators/active", json={"name": "Porch generator"})
            assert r.status_code == 200 and r.json()["active"] == "Porch generator"
            assert (await c.get("/api/state")).json()["total"]["generator_active"] == "Porch generator"
            r = await c.put("/api/generators/active", json={"name": "Nope"})
            assert r.status_code == 404
            # Home Assistant reports the auto-start generator running, contactor still open (warm-up)
            name = "Outhouse generator (auto-start)"
            r = await c.put(f"/api/generators/{name}/signal", json={"running": True, "contactor": False})
            assert r.status_code == 200 and r.json()["running"] is True
            tot = (await c.get("/api/state")).json()["total"]
            g = next(x for x in tot["generators"] if x["name"] == name)
            assert g["running"] is True and g["source"] == "signal" and g["warming_up"] is True
            assert tot["generator_running"] is True
            assert any("cam switch" in w for w in tot["generator_warnings"])  # porch is selected
            r = await c.put(f"/api/generators/{name}/signal", json={"contactor": True})
            g = next(x for x in (await c.get("/api/state")).json()["total"]["generators"] if x["name"] == name)
            assert g["contactor"] is True and g["warming_up"] is False
            r = await c.put(f"/api/generators/{name}/signal", json={"running": False})
            g = next(x for x in (await c.get("/api/state")).json()["total"]["generators"] if x["name"] == name)
            assert g["running"] is False
            summary = (await c.get("/api/generators")).json()
            assert summary["generators"][0]["signal"]["running"] is False
            assert summary["generators"][0]["signal_topics"]["running"].startswith("solardash/generator/")
            r = await c.put("/api/generators/Nope/signal", json={"running": True})
            assert r.status_code == 404
            # weather + forecast + plan through the simulated station
            assert await _wait_for(lambda: (svc.state.weather.get("current") or {}).get("station_id") == "KSIM0001" and svc.state.weather.get("forecast"), timeout=6)
            r = await c.post("/api/forecast/recompute")
            assert r.status_code == 200, r.text
            f = r.json()
            assert f["available"] and len(f["hours"]) >= 72 and f["days"] and f["weather_days"] == 5
            assert f["model"]["k_clear_source"] == "configured"
            assert f["plan"] is not None and "reason" in f["plan"]
            assert f["sources"]["openmeteo"] and f["sources"]["hours_by_source"]["openmeteo"] > 48
            assert (await c.get("/api/devices")).json()["weather:openmeteo"]["connected"] is True
            # occupancy
            r = await c.get("/api/occupancy")
            assert r.json()["mode"] == "away"
            r = await c.put("/api/occupancy", json={"mode": "occupied"})
            assert r.status_code == 200 and (await c.get("/api/occupancy")).json()["mode"] == "occupied"
            assert (await c.put("/api/occupancy", json={"mode": "party"})).status_code == 404
            f = (await c.post("/api/forecast/recompute")).json()
            assert f["occupancy"] == "occupied"
            r = await c.get("/api/scorecard?days=2")
            assert r.status_code == 200 and isinstance(r.json(), list)
            r = await c.get("/api/weather")
            assert r.json()["current"]["solar_radiation"] is not None
            r = await c.get("/")
            assert r.status_code == 200 and b"Watts the Forecast" in r.content
            r = await c.get("/healthz")
            assert r.json()["ok"]


async def test_writes_blocked_when_disabled(cfg):
    cfg.inverter.allow_writes = False
    svc = Service(cfg)
    app = create_app(cfg, svc)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.put("/api/settings/generator_charge_stop_capacity", json={"value": 85})
            assert r.status_code == 403
