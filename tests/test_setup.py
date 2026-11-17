import asyncio
import json

from httpx import ASGITransport, AsyncClient

from solardash.api.app import create_app
from solardash.config import BatteryConfig, Config, apply_overrides, load_config, overrides_path, save_overrides
from solardash.core.service import Service


def _cfg(tmp_path):
    c = Config(simulate=True)
    c.batteries = [BatteryConfig(name="B1", port="sim1", poll_interval=0.5)]
    c.inverter.poll_interval = 0.5
    c.history.path = str(tmp_path / "s.db")
    c.weather.enabled = True
    c.weather.api_key = "secret-key"
    c.weather.station_id = "KSIM0001"
    c.weather.poll_interval = 0.5
    return c


def test_overrides_apply_and_load(tmp_path):
    cfg = Config()
    cfg.history.path = str(tmp_path / "x.db")
    path = overrides_path(cfg)
    save_overrides(path, {"planner": {"soc_floor": 35.0}, "generators": [{"name": "Only one"}], "weather": {"latitude": 39.5, "longitude": -105.0}, "bogus": {"x": 1}})
    apply_overrides(cfg, json.load(open(path)))
    assert cfg.planner.soc_floor == 35.0 and cfg.planner.soc_target == 50.0  # untouched default kept
    assert [g.name for g in cfg.generators] == ["Only one"]
    assert cfg.weather.latitude == 39.5
    # load_config picks them up when the yaml points at the same data dir
    yaml = tmp_path / "config.yaml"
    yaml.write_text(f"history:\n  path: {tmp_path / 'x.db'}\nplanner:\n  soc_floor: 25\n")
    loaded = load_config(yaml)
    assert loaded.planner.soc_floor == 35.0  # override beats the file


async def test_setup_api_updates_and_reconfigures(tmp_path):
    cfg = _cfg(tmp_path)
    svc = Service(cfg)
    app = create_app(cfg, svc)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            d = (await c.get("/api/setup")).json()
            assert d["sections"]["weather"]["values"]["api_key"] == "***"
            assert d["sections"]["weather"]["values"]["station_id"] == "KSIM0001"
            assert {s["key"]: s["kind"] for s in d["sections"]["planner"]["schema"]}["soc_floor"] == "number"
            assert {s["key"]: s["kind"] for s in d["sections"]["weather"]["schema"]}["api_key"] == "password"
            # change planner floor
            r = await c.put("/api/setup", json={"section": "planner", "values": {"soc_floor": 35, "quiet_hours": ["22:00-07:00"]}})
            assert r.status_code == 200, r.text
            assert cfg.planner.soc_floor == 35 and cfg.planner.quiet_hours == ["22:00-07:00"]
            assert "forecast" in r.json()["applied"]
            await asyncio.sleep(0.3)
            f = (await c.post("/api/forecast/recompute")).json()
            assert f["params"]["soc_floor"] == 35
            # weather: mask keeps the secret, station changes, tasks restart
            r = await c.put("/api/setup", json={"section": "weather", "values": {"api_key": "***", "station_id": "KNEW0001"}})
            assert r.status_code == 200 and cfg.weather.api_key == "secret-key" and cfg.weather.station_id == "KNEW0001"
            assert svc.weather is not None and svc.weather.cfg.weather.station_id == "KNEW0001"
            # generators list replaced
            r = await c.put("/api/setup", json={"section": "generators", "values": [{"name": "Big", "notes": "n"}, {"name": "Small"}]})
            assert r.status_code == 200 and [g.name for g in cfg.generators] == ["Big", "Small"]
            assert (await c.get("/api/generators")).json()["active"] == "Big"
            # validation
            assert (await c.put("/api/setup", json={"section": "generators", "values": [{"name": "A"}, {"name": "A"}]})).status_code == 400
            assert (await c.put("/api/setup", json={"section": "planner", "values": {"soc_floor": "abc"}})).status_code == 400
            assert (await c.put("/api/setup", json={"section": "planner", "values": {"soc_floor": 60}})).status_code == 400  # above target
            assert (await c.put("/api/setup", json={"section": "weather", "values": {"latitude": 95}})).status_code == 400
            assert cfg.planner.soc_floor == 35  # rejected writes changed nothing
            assert (await c.put("/api/setup", json={"section": "nope", "values": {}})).status_code == 404
            # persisted
            saved = json.load(open(overrides_path(cfg)))
            assert saved["planner"]["soc_floor"] == 35 and saved["weather"]["station_id"] == "KNEW0001"
            assert saved["weather"].get("api_key") == "secret-key"  # stored so it survives restarts


def test_bad_saved_value_does_not_block_startup(tmp_path):
    cfg = Config()
    cfg.history.path = str(tmp_path / "x.db")
    apply_overrides(cfg, {"planner": {"generator_dc_kw": 30.0, "soc_floor": 33.0}, "generators": [{"name": "A"}, {"name": "A"}]})
    assert cfg.planner.generator_dc_kw == 2.4 and cfg.planner.soc_floor == 33.0  # bad value dropped, good one kept
    assert len(cfg.generators) == 2  # invalid list ignored, defaults kept


def test_old_saved_poll_default_is_upgraded(tmp_path):
    cfg = Config()
    apply_overrides(cfg, {"inverter": {"poll_interval": 5.0, "baud": 19200},
                          "batteries": [{"name": "B1", "port": "/dev/ttyACM1", "poll_interval": 5.0}, {"name": "B2", "port": "/dev/ttyACM2", "poll_interval": 3.0}]})
    assert cfg.inverter.poll_interval == 2.0
    assert [b.poll_interval for b in cfg.batteries] == [2.0, 3.0]  # a deliberate custom value is kept
