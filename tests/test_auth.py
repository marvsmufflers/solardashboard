import base64

from httpx import ASGITransport, AsyncClient

from solardash.api.app import create_app
from solardash.config import BatteryConfig, Config
from solardash.core.service import Service


def _cfg(tmp_path, password):
    c = Config(simulate=True)
    c.batteries = [BatteryConfig(name="B1", port="sim1", poll_interval=0.5)]
    c.inverter.poll_interval = 0.5
    c.history.path = str(tmp_path / "a.db")
    c.web.password = password
    return c


async def test_open_when_no_password(tmp_path):
    cfg = _cfg(tmp_path, "")
    app = create_app(cfg, Service(cfg))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get("/api/state")).status_code == 200
            assert (await c.get("/")).status_code == 200
            assert (await c.get("/login")).status_code == 404


async def test_password_gate(tmp_path):
    cfg = _cfg(tmp_path, "hunter2")
    app = create_app(cfg, Service(cfg))
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            # open paths
            assert (await c.get("/healthz")).status_code == 200
            assert (await c.get("/static/style.css")).status_code == 200
            # gated
            r = await c.get("/api/state")
            assert r.status_code == 401
            r = await c.get("/", follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"].startswith("/login")
            assert (await c.get("/login")).status_code == 200
            # wrong password
            r = await c.post("/login", data={"password": "nope", "next": "/"})
            assert r.status_code == 401 and "Wrong password" in r.text
            # right password -> cookie -> access
            r = await c.post("/login", data={"password": "hunter2", "next": "/?x=1"}, follow_redirects=False)
            assert r.status_code == 303 and r.headers["location"] == "/?x=1"
            assert "solardash_session" in r.cookies
            assert (await c.get("/api/state")).status_code == 200
            assert (await c.get("/")).status_code == 200
            # open redirect blocked
            r = await c.post("/login", data={"password": "hunter2", "next": "//evil.example"}, follow_redirects=False)
            assert r.headers["location"] == "/"
            # logout
            r = await c.get("/logout", follow_redirects=False)
            assert r.status_code == 303
            c.cookies.clear()
            assert (await c.get("/api/state")).status_code == 401
            # bearer and basic for scripts / Home Assistant
            assert (await c.get("/api/state", headers={"Authorization": "Bearer hunter2"})).status_code == 200
            assert (await c.get("/api/state", headers={"Authorization": "Bearer wrong"})).status_code == 401
            basic = base64.b64encode(b"anyone:hunter2").decode()
            assert (await c.get("/api/state", headers={"Authorization": f"Basic {basic}"})).status_code == 200
            # secrets never leak through /api/config
            d = (await c.get("/api/config", headers={"Authorization": "Bearer hunter2"})).json()
            assert d["web"]["password"] == "***"


def test_session_cookie_always_verifies():
    from solardash.api.auth import PasswordGate

    g = PasswordGate("pw")
    import time as _t

    base = int(_t.time())
    # every issue time over a day must round-trip (signatures with a '.' byte used to fail)
    assert all(g.verify_cookie(g._token(base - k)) for k in range(0, 86400, 37))
