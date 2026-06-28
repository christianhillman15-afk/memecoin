"""FastAPI application: REST + WebSocket API and static dashboard hosting."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config
from .database import Database
from .engine.scanner import Scanner

STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


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
        yield
        await scanner.stop()
        db.close()

    app = FastAPI(title="MemeRadar", version="1.0.0", lifespan=lifespan)
    app.state.cfg = cfg
    app.state.db = db
    app.state.scanner = scanner

    # ---- pages ---------------------------------------------------------- #
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

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
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


app = create_app()
