"""Password-only dashboard login via a signed session cookie.

HTTP Basic auth always forces a username field in the browser dialog; this gives
a clean single-password login screen instead. The session cookie is a stateless
HMAC token keyed off the password, so it needs no server-side store and survives
restarts (until the password changes). Cookies are sent on same-origin
WebSocket handshakes too, so the live feed is gated and still works once logged
in.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from http.cookies import SimpleCookie
from typing import Callable

COOKIE_NAME = "mr_session"
SESSION_TTL = 30 * 24 * 3600  # 30 days


def make_session(password: str) -> tuple[Callable[[], str], Callable[[str], bool]]:
    """Return (issue, verify) bound to a key derived from the password."""
    key = hashlib.sha256(("trenchr-session-v1:" + password).encode()).digest()

    def _sig(expiry: int) -> str:
        return hmac.new(key, str(expiry).encode(), hashlib.sha256).hexdigest()

    def issue() -> str:
        expiry = int(time.time()) + SESSION_TTL
        return f"{expiry}.{_sig(expiry)}"

    def verify(token: str) -> bool:
        try:
            exp_str, sig = token.split(".", 1)
            expiry = int(exp_str)
        except (ValueError, AttributeError):
            return False
        if expiry < time.time():
            return False
        return hmac.compare_digest(sig, _sig(expiry))

    return issue, verify


def _cookie_token(scope) -> str | None:
    for name, value in scope.get("headers") or []:
        if name == b"cookie":
            jar = SimpleCookie()
            try:
                jar.load(value.decode("latin-1"))
            except Exception:  # noqa: BLE001
                return None
            morsel = jar.get(COOKIE_NAME)
            return morsel.value if morsel else None
    return None


class SessionAuthMiddleware:
    """Gate all routes (except the login endpoints) behind a session cookie."""

    OPEN_PATHS = {"/login", "/logout"}

    def __init__(self, app, verify: Callable[[str], bool], enabled: bool):
        self.app = app
        self.verify = verify
        self.enabled = enabled

    async def __call__(self, scope, receive, send):
        if not self.enabled or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in self.OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        token = _cookie_token(scope)
        if token and self.verify(token):
            await self.app(scope, receive, send)
            return
        # unauthenticated
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if path.startswith("/api"):
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"error":"login required"}'})
            return
        # redirect browsers to the login page
        await send({"type": "http.response.start", "status": 303,
                    "headers": [(b"location", b"/login")]})
        await send({"type": "http.response.body", "body": b""})


def login_page(error: bool = False) -> str:
    msg = ('<p class="err">Incorrect password</p>' if error else "")
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Trenchr — Login</title><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{height:100vh;display:grid;place-items:center;font-family:-apple-system,
BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:radial-gradient(1000px 500px at 70% -10%,#13203a,transparent 55%),#0a0e17;color:#e6edf6}}
.card{{background:linear-gradient(180deg,#121a28,#0d1420);border:1px solid #1e2a3d;
border-radius:16px;padding:34px 32px;width:340px;box-shadow:0 8px 30px rgba(0,0,0,.45);text-align:center}}
.logo{{width:56px;height:56px;border-radius:14px;display:grid;place-items:center;font-size:30px;
font-weight:800;color:#04110c;background:linear-gradient(135deg,#19e3a4,#37d6d6);margin:0 auto 16px}}
h1{{font-size:19px;margin-bottom:4px}} p.sub{{color:#7d8ba3;font-size:12.5px;margin-bottom:20px}}
input{{width:100%;background:#0d1420;border:1px solid #26344a;border-radius:9px;color:#e6edf6;
padding:12px 14px;font-size:14px;outline:none;margin-bottom:12px}}
input:focus{{border-color:#19e3a4}}
button{{width:100%;background:linear-gradient(135deg,#19e3a4,#37d6d6);color:#04110c;border:none;
border-radius:9px;padding:12px;font-size:14px;font-weight:700;cursor:pointer}}
button:hover{{opacity:.92}} .err{{color:#ff5267;font-size:12.5px;margin-bottom:12px}}
</style></head><body><div class="card">
<div class="logo">◎</div><h1>Trenchr</h1>
<p class="sub">Enter password to access the desk</p>{msg}
<form method="POST" action="/login">
<input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password">
<button type="submit">Unlock</button></form></div></body></html>"""
