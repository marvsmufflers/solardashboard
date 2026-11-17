"""Optional password gate for the web UI and API.

Off unless ``web.password`` (or ``$SOLARDASH_PASSWORD``) is set. Meant as a
second layer behind Cloudflare Access / Tailscale, not as the only one.

* Browser: ``/login`` form sets a signed, HttpOnly cookie for 30 days.
* Scripts / Home Assistant REST: ``Authorization: Bearer <password>`` or HTTP
  Basic with any username.
* ``/healthz`` and ``/static/*`` stay open.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

COOKIE = "solardash_session"
COOKIE_TTL = 30 * 86400
OPEN_PREFIXES = ("/healthz", "/static/", "/login", "/logout", "/favicon.ico", "/apple-touch-icon")

LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Watts the Forecast</title><link rel="icon" href="/static/favicon.ico"><link rel="stylesheet" href="/static/style.css"><link rel="manifest" href="/static/manifest.json">
<link rel="apple-touch-icon" href="/static/icon-180.png"><meta name="apple-mobile-web-app-capable" content="yes">
<style>.login{max-width:360px;margin:12vh auto 0;padding:0 16px}.login .card{padding:22px}.login .hero{display:block;width:100%;height:auto;border-radius:12px;margin:0 0 16px}.login p{margin:0 0 14px}
.login input{width:100%;padding:12px 14px;font-size:17px;border-radius:12px;border:1px solid var(--line);background:var(--card-2);color:var(--text);margin-bottom:10px}
.login button{width:100%;padding:12px;font-size:16px;font-weight:600;background:var(--accent);color:#111;border:0;border-radius:12px}.err{color:var(--bad);font-size:13px;margin-bottom:10px}</style></head>
<body><main class="login"><div class="card"><picture><source srcset="/static/logo-dark.webp" media="(prefers-color-scheme: dark)"><img class="hero" src="/static/logo.webp" alt="Watts the Forecast" width="960" height="640"></picture><p class="muted">Enter the dashboard password.</p>
{error}<form method="post" action="/login"><input type="password" name="password" autocomplete="current-password" autofocus placeholder="Password">
<input type="hidden" name="next" value="{next}"><button type="submit">Sign in</button></form></div></main></body></html>"""


class PasswordGate:
    def __init__(self, password: str) -> None:
        self.password = password
        self._key = hashlib.sha256(("solardash-session:" + password).encode()).digest()

    def _token(self, issued: int) -> str:
        msg = str(issued).encode()
        sig = hmac.new(self._key, msg, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(msg + b"." + sig).decode().rstrip("=")

    def issue(self) -> str:
        return self._token(int(time.time()))

    def verify_cookie(self, value: str | None) -> bool:
        if not value:
            return False
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            msg, sig = raw.split(b".", 1)  # msg is digits only; the binary signature may contain '.'
            issued = int(msg)
        except (ValueError, TypeError):
            return False
        expected = hmac.new(self._key, msg, hashlib.sha256).digest()
        return hmac.compare_digest(sig, expected) and time.time() - issued < COOKIE_TTL

    def verify_header(self, auth: str | None) -> bool:
        if not auth:
            return False
        scheme, _, rest = auth.partition(" ")
        if scheme.lower() == "bearer":
            return secrets.compare_digest(rest.strip(), self.password)
        if scheme.lower() == "basic":
            try:
                decoded = base64.b64decode(rest.strip()).decode()
            except (ValueError, UnicodeDecodeError):
                return False
            return secrets.compare_digest(decoded.partition(":")[2], self.password)
        return False

    def allowed(self, request: Request) -> bool:
        return self.verify_cookie(request.cookies.get(COOKIE)) or self.verify_header(request.headers.get("authorization"))


def _secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def install_password_gate(app: FastAPI, password: str) -> PasswordGate:
    gate = PasswordGate(password)

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        path = request.url.path
        if path.startswith(OPEN_PREFIXES) or gate.allowed(request):
            return await call_next(request)
        if path.startswith("/api/") or path == "/ws":
            return JSONResponse({"detail": "authentication required"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        return RedirectResponse(f"/login?next={path}", status_code=303)

    @app.get("/login", include_in_schema=False)
    async def login_page(next: str = "/"):
        return HTMLResponse(LOGIN_HTML.replace("{error}", "").replace("{next}", next if next.startswith("/") else "/"))

    @app.post("/login", include_in_schema=False)
    async def login_submit(request: Request):
        form = await request.form()
        pw = str(form.get("password", ""))
        nxt = str(form.get("next", "/"))
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/"
        if not secrets.compare_digest(pw, password):
            time.sleep(0.8)  # slow brute force a little
            return HTMLResponse(LOGIN_HTML.replace("{error}", '<div class="err">Wrong password.</div>').replace("{next}", nxt), status_code=401)
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(COOKIE, gate.issue(), max_age=COOKIE_TTL, httponly=True, samesite="lax", secure=_secure(request), path="/")
        return resp

    @app.get("/logout", include_in_schema=False)
    async def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    return gate


def websocket_allowed(gate: PasswordGate | None, cookies: dict, headers) -> bool:
    if gate is None:
        return True
    return gate.verify_cookie(cookies.get(COOKIE)) or gate.verify_header(headers.get("authorization"))


def no_store(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-store"
    return resp
