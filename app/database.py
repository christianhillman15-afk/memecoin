"""Thread-safe SQLite persistence for trades, signals and the equity curve."""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from .models import Signal, Trade


class Database:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    address TEXT, symbol TEXT, name TEXT,
                    qty REAL, entry_price REAL, exit_price REAL,
                    entry_value REAL, exit_value REAL,
                    pnl REAL, pnl_pct REAL,
                    opened_at REAL, closed_at REAL,
                    entry_reason TEXT, exit_reason TEXT, meta TEXT
                );
                CREATE TABLE IF NOT EXISTS equity_curve (
                    ts REAL PRIMARY KEY,
                    equity REAL, cash REAL, realized_pnl REAL, unrealized_pnl REAL
                );
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL, kind TEXT, severity TEXT,
                    token TEXT, symbol TEXT, message TEXT, meta TEXT
                );
                CREATE TABLE IF NOT EXISTS positions (
                    address TEXT PRIMARY KEY,
                    data TEXT
                );
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            self._conn.commit()
            self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after the original schema (idempotent)."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(trades)").fetchall()}
        if "meta" not in cols:
            try:
                self._conn.execute("ALTER TABLE trades ADD COLUMN meta TEXT")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass

    # --- generic key/value (portfolio cash, counters) --------------------- #
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            self._conn.commit()

    # --- trades ----------------------------------------------------------- #
    def insert_trade(self, t: Trade) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO trades
                   (address,symbol,name,qty,entry_price,exit_price,entry_value,
                    exit_value,pnl,pnl_pct,opened_at,closed_at,entry_reason,exit_reason,meta)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (t.address, t.symbol, t.name, t.qty, t.entry_price, t.exit_price,
                 t.entry_value, t.exit_value, t.pnl, t.pnl_pct, t.opened_at,
                 t.closed_at, t.entry_reason, t.exit_reason,
                 json.dumps(t.entry_context or {})),
            )
            self._conn.commit()

    def recent_trades(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trades ORDER BY closed_at DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["entry_context"] = json.loads(d.get("meta") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["entry_context"] = {}
            d.pop("meta", None)
            out.append(d)
        return out

    def trade_stats(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """SELECT
                     COUNT(*) AS n,
                     COALESCE(SUM(pnl),0) AS realized,
                     COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0) AS wins,
                     COALESCE(SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END),0) AS losses
                   FROM trades"""
            ).fetchone()
        return {"n": row["n"], "realized": row["realized"],
                "wins": row["wins"], "losses": row["losses"]}

    # --- equity curve ----------------------------------------------------- #
    def insert_equity(self, ts: float, equity: float, cash: float,
                      realized: float, unrealized: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO equity_curve VALUES (?,?,?,?,?)",
                (ts, equity, cash, realized, unrealized),
            )
            self._conn.commit()

    def equity_curve(self, limit: int = 1000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM (SELECT * FROM equity_curve ORDER BY ts DESC LIMIT ?) "
                "ORDER BY ts ASC", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- signals ---------------------------------------------------------- #
    def insert_signal(self, s: Signal) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO signals(ts,kind,severity,token,symbol,message,meta) "
                "VALUES (?,?,?,?,?,?,?)",
                (s.ts, s.kind, s.severity, s.token, s.symbol, s.message,
                 json.dumps(s.meta)),
            )
            self._conn.commit()

    def recent_signals(self, limit: int = 80) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM signals ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["meta"] = json.loads(d.get("meta") or "{}")
            except json.JSONDecodeError:
                d["meta"] = {}
            out.append(d)
        return out

    # --- positions (persisted for restart resilience) -------------------- #
    def save_positions(self, positions: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM positions")
            self._conn.executemany(
                "INSERT INTO positions(address,data) VALUES (?,?)",
                [(addr, json.dumps(data)) for addr, data in positions.items()],
            )
            self._conn.commit()

    def load_positions(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT address,data FROM positions").fetchall()
        return {r["address"]: json.loads(r["data"]) for r in rows}

    def reset(self) -> None:
        with self._lock:
            self._conn.executescript(
                "DELETE FROM trades; DELETE FROM equity_curve; DELETE FROM signals; "
                "DELETE FROM positions; DELETE FROM kv;"
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
