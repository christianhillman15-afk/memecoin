"""FastAPI application: REST + WebSocket API and static dashboard hosting."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse)
from fastapi.staticfiles import StaticFiles

from .auth import COOKIE_NAME, SESSION_TTL, SessionAuthMiddleware, login_page, make_session
from .config import load_config
from .database import Database
from .engine.scanner import Scanner
from .telegram import build_telegram_bot

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


class NoCacheStaticFiles(StaticFiles):
    """Serve static assets with revalidation so a rebuild is picked up
    immediately instead of the browser running a stale cached app.js."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


class ConnectionManager:
    def __init__(self) -> None:
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.active.discard(ws)

    async def broadcast(self, message: dict[str, Any]) -> None:
        dead = []
        data = json.dumps(message, default=str)
        for ws in list(self.active):
            try:
                await ws.send_text(data)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


def create_app() -> FastAPI:
    cfg = load_config()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    db = Database(cfg.db_path)
    scanner = Scanner(cfg, db)
    manager = ConnectionManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        def _push() -> None:
            asyncio.create_task(manager.broadcast({"type": "snapshot",
                                                   "data": scanner.snapshot()}))
        scanner.on_update(_push)
        scanner.start()
        # optional Telegram bot — only built when a token + allowlist are set;
        # a Telegram failure must never block app startup or scanner shutdown
        bot = build_telegram_bot(cfg, scanner, db)
        app.state.telegram = bot
        if bot:
            try:
                await bot.start()
            except Exception:  # noqa: BLE001
                logging.getLogger("memeradar.telegram").exception("Telegram start failed")
        yield
        if bot:
            try:
                await bot.stop()
            except Exception:  # noqa: BLE001
                pass
        await scanner.stop()
        db.close()

    app = FastAPI(title="MemeRadar", version="1.0.0", lifespan=lifespan)
    app.state.cfg = cfg
    app.state.db = db
    app.state.scanner = scanner

    issue_session, verify_session = make_session(cfg.dashboard_password)
    if cfg.auth_enabled:
        app.add_middleware(SessionAuthMiddleware, verify=verify_session,
                           enabled=True)
        logging.getLogger("memeradar").info("Dashboard login enabled (password only)")

    @app.get("/login")
    async def login_get(error: int = 0) -> HTMLResponse:
        return HTMLResponse(login_page(error=bool(error)))

    @app.post("/login")
    async def login_post(request: Request) -> RedirectResponse:
        body = (await request.body()).decode("utf-8", "ignore")
        password = parse_qs(body).get("password", [""])[0]
        if cfg.auth_enabled and secrets.compare_digest(password, cfg.dashboard_password):
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE_NAME, issue_session(), max_age=SESSION_TTL,
                            httponly=True, samesite="lax")
            return resp
        return RedirectResponse("/login?error=1", status_code=303)

    @app.get("/logout")
    async def logout() -> RedirectResponse:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE_NAME)
        return resp

    # ---- pages ---------------------------------------------------------- #
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html",
                            headers={"Cache-Control": "no-cache, must-revalidate"})

    # ---- REST ----------------------------------------------------------- #
    @app.get("/api/snapshot")
    async def api_snapshot() -> JSONResponse:
        return JSONResponse(scanner.snapshot())

    @app.get("/api/equity")
    async def api_equity() -> JSONResponse:
        return JSONResponse(db.equity_curve(1500))

    @app.get("/api/trades")
    async def api_trades() -> JSONResponse:
        return JSONResponse(db.recent_trades(150))

    @app.get("/api/signals")
    async def api_signals() -> JSONResponse:
        return JSONResponse(db.recent_signals(120))

    @app.get("/api/wallets")
    async def api_wallets() -> JSONResponse:
        return JSONResponse(scanner.intel.top_wallets(18))

    @app.get("/api/wallets/categorized")
    async def api_wallets_categorized() -> JSONResponse:
        return JSONResponse(scanner.intel.categorized(14))

    @app.get("/api/wallets/all")
    async def api_wallets_all() -> JSONResponse:
        return JSONResponse(scanner.intel.wallet_list())

    @app.get("/api/bundles")
    async def api_bundles() -> JSONResponse:
        return JSONResponse(scanner.intel.bundles(24))

    @app.get("/api/loadouts")
    async def api_loadouts() -> JSONResponse:
        return JSONResponse(scanner.intel.loadouts(14))

    @app.get("/api/cabals/accumulating")
    async def api_cabals_accum() -> JSONResponse:
        return JSONResponse(scanner.intel.accumulating_cabals(8))

    @app.get("/api/wallet/{address}")
    async def api_wallet(address: str) -> JSONResponse:
        return JSONResponse(scanner.intel.wallet_profile(address))

    @app.get("/api/cabal/{cabal_id}")
    async def api_cabal(cabal_id: str) -> JSONResponse:
        profile = scanner.intel.cabal_profile(cabal_id)
        if profile is None:
            return JSONResponse({"error": "cabal not found"}, status_code=404)
        return JSONResponse(profile)

    @app.get("/api/influencers")
    async def api_influencers() -> JSONResponse:
        return JSONResponse(scanner.influencers.snapshot())

    @app.get("/api/config")
    async def api_config() -> JSONResponse:
        return JSONResponse(cfg.public_dict())

    # ---- controls ------------------------------------------------------- #
    @app.post("/api/control/pause")
    async def ctrl_pause() -> dict[str, Any]:
        scanner.paused = True
        return {"paused": scanner.paused}

    @app.post("/api/control/resume")
    async def ctrl_resume() -> dict[str, Any]:
        scanner.paused = False
        return {"paused": scanner.paused}

    @app.post("/api/control/reset")
    async def ctrl_reset() -> dict[str, Any]:
        scanner.trader.reset()
        scanner.trader.record_equity_point()
        return {"ok": True, "cash": scanner.trader.cash}

    @app.post("/api/control/scan")
    async def ctrl_scan() -> dict[str, Any]:
        try:
            await scanner.scan_once()
            await manager.broadcast({"type": "snapshot", "data": scanner.snapshot()})
            return {"ok": True, "scan_count": scanner.scan_count}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    # ---- websocket ------------------------------------------------------ #
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await manager.connect(ws)
        try:
            await ws.send_text(json.dumps({"type": "snapshot",
                                           "data": scanner.snapshot()}, default=str))
            while True:
                await ws.receive_text()  # keepalive / ignore inbound
        except WebSocketDisconnect:
            manager.disconnect(ws)
        except Exception:  # noqa: BLE001
            manager.disconnect(ws)

    # static assets (js/css) — mounted last so "/" stays our index
    app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


app = create_app()
