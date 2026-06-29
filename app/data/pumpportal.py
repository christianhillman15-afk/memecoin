"""PumpPortal live ingester — streams brand-new pump.fun coins + graduations.

The free, key-less websocket (``wss://pumpportal.fun/api/data``) streams every
new pump.fun coin creation and every migration ("graduation" to Raydium/PumpSwap)
with no API key. We use it to catch coins in their **first seconds**: the mint,
the **creator wallet**, the initial market cap, the bonding-curve reserves and
the socials URI. DexScreener then supplies the live price / liquidity / volume we
score and mark with.

From this free stream we auto-discover **creator wallets** and track which of
their coins gain traction or graduate — real, free wallet intelligence on the
people launching coins.

A funded ``PUMPPORTAL_API_KEY`` additionally unlocks the per-trade buyer stream
(``subscribeTokenTrade``), which — when present — we use to attribute individual
buyer wallets. Without it, everything above still works.

Everything is read between awaits on the single asyncio loop, so no locking is
needed; callers that iterate take a shallow copy first.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from typing import Any, Optional

import websockets

from ..config import Config
from ..models import now

log = logging.getLogger("memeradar.pumpportal")

WS_URL = "wss://pumpportal.fun/api/data"
# pump.fun total supply is fixed at 1,000,000,000 tokens
PUMP_TOTAL_SUPPLY = 1_000_000_000


class PumpPortalIngester:
    """Maintains a rolling view of brand-new pump.fun coins and their creators."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._coins: dict[str, dict[str, Any]] = {}     # mint -> record
        self._order: deque[str] = deque()               # mints, oldest first (eviction)
        self._migrations: deque[dict[str, Any]] = deque(maxlen=200)
        self._creators: dict[str, dict[str, Any]] = {}  # wallet -> track record
        self._buyers: dict[str, dict[str, Any]] = {}    # wallet -> trade-stream record (key only)
        self._tracked_trades: set[str] = set()          # mints we asked for trades on
        self._pending_trade_subs: list[str] = []        # mints awaiting a trade subscription

        self.total_coins_seen = 0
        self.total_migrations = 0
        self.total_trades = 0
        self.connected = False
        self.last_event_ts: float = 0.0

        self._task: Optional[asyncio.Task] = None
        self._ws: Any = None
        self._running = False

    # --- lifecycle -------------------------------------------------------- #
    def start(self) -> None:
        if not self.cfg.launchpad_enabled:
            log.info("Launchpad disabled (launchpad_enabled: false)")
            return
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @property
    def _url(self) -> str:
        if self.cfg.has_pumpportal_trades:
            return f"{WS_URL}?api-key={self.cfg.pumpportal_api_key}"
        return WS_URL

    async def _run(self) -> None:
        backoff = 2.0
        while self._running:
            try:
                async with websockets.connect(
                    self._url, open_timeout=20, ping_interval=20, ping_timeout=20,
                    max_queue=4096,
                ) as ws:
                    self._ws = ws
                    self.connected = True
                    backoff = 2.0
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    await ws.send(json.dumps({"method": "subscribeMigration"}))
                    log.info("PumpPortal connected (%s)",
                             "trades+new+migrate" if self.cfg.has_pumpportal_trades
                             else "new+migrate, free")
                    async for raw in ws:
                        if not self._running:
                            break
                        self._handle(raw)
                        if self._pending_trade_subs:
                            await self._flush_trade_subs(ws)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 — keep reconnecting
                log.warning("PumpPortal connection dropped: %s", e)
            finally:
                self.connected = False
                self._ws = None
            if self._running:
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)

    # --- message handling ------------------------------------------------- #
    def _handle(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if "message" in msg and "txType" not in msg:
            return  # subscription ack / info
        tt = msg.get("txType")
        if tt == "create":
            self._on_create(msg)
        elif tt == "migrate":
            self._on_migrate(msg)
        elif tt in ("buy", "sell"):
            self._on_trade(msg)

    def _on_create(self, m: dict[str, Any]) -> None:
        mint = m.get("mint")
        if not mint or mint in self._coins:
            return
        creator = m.get("traderPublicKey", "")
        rec = {
            "mint": mint,
            "name": (m.get("name") or "")[:60],
            "symbol": (m.get("symbol") or "?")[:16],
            "creator": creator,
            "created_at": now(),
            "init_mcap_sol": float(m.get("marketCapSol") or 0),
            "sol_reserve": float(m.get("vSolInBondingCurve") or 0),
            "token_reserve": float(m.get("vTokensInBondingCurve") or 0),
            "initial_buy": float(m.get("initialBuy") or 0),
            "uri": m.get("uri") or "",
            "pool": m.get("pool") or "pump",
            "signature": m.get("signature") or "",
            # filled in later by the engine via note_traction()
            "migrated": False,
            "traction": False,
            "best_liq": 0.0,
            "best_vol_h1": 0.0,
            "buyers": [],
        }
        self._coins[mint] = rec
        self._order.append(mint)
        self.total_coins_seen += 1
        self.last_event_ts = rec["created_at"]
        self._bump_creator(creator, mint)
        self._evict()
        # queue a trade subscription (only sent when a funded key is present);
        # the async WS loop flushes the queue so we never await from here
        if self.cfg.has_pumpportal_trades and mint not in self._tracked_trades:
            self._pending_trade_subs.append(mint)

    def _on_migrate(self, m: dict[str, Any]) -> None:
        mint = m.get("mint")
        self.total_migrations += 1
        self.last_event_ts = now()
        self._migrations.appendleft({"mint": mint, "ts": now(),
                                     "pool": m.get("pool") or ""})
        rec = self._coins.get(mint) if mint else None
        if rec and not rec["migrated"]:
            rec["migrated"] = True
            self._credit_creator(rec["creator"], graduated=True)
            self._credit_buyers(mint, graduated=True)

    def _on_trade(self, m: dict[str, Any]) -> None:
        # only reachable with a funded key; attribute the buyer wallet
        self.total_trades += 1
        wallet = m.get("traderPublicKey")
        mint = m.get("mint")
        if not wallet:
            return
        side = m.get("txType")
        sol = float(m.get("solAmount") or 0)
        b = self._buyers.get(wallet)
        if b is None:
            b = {"wallet": wallet, "buys": 0, "sells": 0, "sol_in": 0.0, "sol_out": 0.0,
                 "coins": set(), "wins": set(), "grads": set(),
                 "first_seen": now(), "last_seen": now()}
            self._buyers[wallet] = b
            if len(self._buyers) > 8000:
                self._trim_buyers()
        b["last_seen"] = now()
        if side == "buy":
            b["buys"] += 1
            b["sol_in"] += sol
            if mint:
                b["coins"].add(mint)            # only count coins they actually bought
        else:
            b["sells"] += 1
            b["sol_out"] += sol
        rec = self._coins.get(mint) if mint else None
        if rec is not None and side == "buy" and wallet not in rec["buyers"]:
            if len(rec["buyers"]) < 80:
                rec["buyers"].append(wallet)

    def _credit_buyers(self, mint: str, *, traction: bool = False,
                       graduated: bool = False) -> None:
        """When a coin wins, credit the wallets that bought it early — the real
        signal that a wallet 'gets into winners'. Idempotent (set-backed)."""
        rec = self._coins.get(mint)
        if not rec:
            return
        for w in rec.get("buyers", []):
            b = self._buyers.get(w)
            if not b:
                continue
            if traction or graduated:
                b["wins"].add(mint)
            if graduated:
                b["grads"].add(mint)

    def _trim_buyers(self) -> None:
        victims = [w for w, b in self._buyers.items()
                   if len(b["coins"]) <= 1 and not b["wins"]
                   and (b["sol_in"] - b["sol_out"]) <= 0]
        victims.sort(key=lambda w: self._buyers[w]["last_seen"])
        for w in victims[:3000]:
            self._buyers.pop(w, None)

    async def _flush_trade_subs(self, ws: Any) -> None:
        """Send queued token-trade subscriptions in one batched message."""
        keys = [m for m in self._pending_trade_subs[:80] if m not in self._tracked_trades]
        self._pending_trade_subs = self._pending_trade_subs[80:]
        if not keys:
            return
        try:
            await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": keys}))
            self._tracked_trades.update(keys)
        except Exception:  # noqa: BLE001 — will retry on the next create
            pass

    # --- creator track records ------------------------------------------- #
    def _bump_creator(self, wallet: str, mint: str) -> None:
        if not wallet:
            return
        c = self._creators.get(wallet)
        if c is None:
            c = {"wallet": wallet, "launches": 0, "graduated": 0, "tractions": 0,
                 "mints": deque(maxlen=12), "first_seen": now(), "last_seen": now()}
            self._creators[wallet] = c
        c["launches"] += 1
        c["last_seen"] = now()
        c["mints"].append(mint)
        # bound the creator table so a long-running process can't grow unbounded
        if len(self._creators) > 6000:
            self._trim_creators()

    def _credit_creator(self, wallet: str, *, graduated: bool = False,
                        traction: bool = False) -> None:
        c = self._creators.get(wallet)
        if not c:
            return
        if graduated:
            c["graduated"] += 1
        if traction:
            c["tractions"] += 1

    def _trim_creators(self) -> None:
        # drop one-shot creators with no success, oldest first
        victims = [w for w, c in self._creators.items()
                   if c["launches"] <= 1 and c["graduated"] == 0 and c["tractions"] == 0]
        victims.sort(key=lambda w: self._creators[w]["last_seen"])
        for w in victims[:2000]:
            self._creators.pop(w, None)

    def _evict(self) -> None:
        cutoff = now() - self.cfg.launchpad_keep_minutes * 60
        while self._order:
            mint = self._order[0]
            rec = self._coins.get(mint)
            if rec and rec["created_at"] >= cutoff:
                break
            self._order.popleft()
            self._coins.pop(mint, None)
            self._tracked_trades.discard(mint)

    # --- engine feedback -------------------------------------------------- #
    def note_traction(self, mint: str, liq_usd: float, vol_h1_usd: float) -> None:
        """Engine reports live market data so we can mark a creator 'hit'."""
        rec = self._coins.get(mint)
        if not rec:
            return
        rec["best_liq"] = max(rec["best_liq"], liq_usd)
        rec["best_vol_h1"] = max(rec["best_vol_h1"], vol_h1_usd)
        if not rec["traction"] and (
                liq_usd >= self.cfg.creator_traction_liq_usd
                or vol_h1_usd >= self.cfg.creator_traction_vol_usd):
            rec["traction"] = True
            self._credit_creator(rec["creator"], traction=True)
            self._credit_buyers(mint, traction=True)

    # --- accessors (callers copy first; reads are between awaits) --------- #
    def recent_coins(self, max_age_minutes: Optional[float] = None) -> list[dict[str, Any]]:
        max_age = (max_age_minutes if max_age_minutes is not None
                   else self.cfg.launchpad_max_age_minutes)
        cutoff = now() - max_age * 60
        return [dict(r) for r in self._coins.values() if r["created_at"] >= cutoff]

    def coin(self, mint: str) -> Optional[dict[str, Any]]:
        rec = self._coins.get(mint)
        return dict(rec) if rec else None

    def creator_record(self, wallet: str) -> Optional[dict[str, Any]]:
        c = self._creators.get(wallet)
        if not c:
            return None
        return self._creator_view(c)

    def discovered_creators(self, limit: int = 40) -> list[dict[str, Any]]:
        """Creators worth surfacing: repeat launchers or any with a success."""
        out = []
        for c in self._creators.values():
            if c["launches"] >= 2 or c["graduated"] or c["tractions"]:
                out.append(self._creator_view(c))
        out.sort(key=lambda c: (c["graduated"], c["tractions"], c["hit_rate"],
                                c["launches"]), reverse=True)
        return out[:limit]

    def discovered_buyers(self, limit: int = 40) -> list[dict[str, Any]]:
        """Per-trade buyer wallets, smart-scored (only populated with a funded
        key). Ranked by how reliably they buy coins that go on to win."""
        out = []
        for b in self._buyers.values():
            if not b["coins"]:
                continue
            out.append(self._buyer_view(b))
        out.sort(key=lambda b: (b["smart_score"], b["wins"]), reverse=True)
        return out[:limit]

    def buyer_record(self, wallet: str) -> Optional[dict[str, Any]]:
        b = self._buyers.get(wallet)
        return self._buyer_view(b) if b else None

    @staticmethod
    def _buyer_view(b: dict[str, Any]) -> dict[str, Any]:
        coins = len(b["coins"])
        wins = len(b["wins"])
        grads = len(b["grads"])
        net = b["sol_in"] - b["sol_out"]
        return {
            "wallet": b["wallet"], "wallet_short": _short(b["wallet"]),
            "smart_score": buyer_smart_score(b["buys"], b["sells"], b["sol_in"],
                                             b["sol_out"], coins, wins, grads),
            "hit_rate": round(min(1.0, wins / max(1, coins)) * 100, 0),
            "wins": wins, "grads": grads, "coins": coins,
            "buys": b["buys"], "sells": b["sells"],
            "sol_in": round(b["sol_in"], 3), "sol_out": round(b["sol_out"], 3),
            "net_sol": round(net, 3),
            "first_seen": b["first_seen"], "last_seen": b["last_seen"],
        }

    @staticmethod
    def _creator_view(c: dict[str, Any]) -> dict[str, Any]:
        launches = max(1, c["launches"])
        hit = (c["graduated"] + c["tractions"]) / launches
        return {
            "wallet": c["wallet"], "wallet_short": _short(c["wallet"]),
            "launches": c["launches"], "graduated": c["graduated"],
            "tractions": c["tractions"], "hit_rate": round(min(1.0, hit) * 100, 0),
            "mints": list(c["mints"]), "first_seen": c["first_seen"],
            "last_seen": c["last_seen"],
        }

    def stats(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "coins_seen": self.total_coins_seen,
            "migrations": self.total_migrations,
            "trades_seen": self.total_trades,
            "tracked_now": len(self._coins),
            "creators_known": len(self._creators),
            "buyers_known": len(self._buyers),
            "trade_stream": self.cfg.has_pumpportal_trades,
            "last_event_ts": self.last_event_ts,
        }


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def buyer_smart_score(buys: int, sells: int, sol_in: float, sol_out: float,
                      coins: int, wins: int, grads: int) -> float:
    """Rank a buyer wallet 0-100 by how reliably it gets into winners early.

    The strongest signal is real: the share of the wallet's bought coins that
    went on to gain traction / graduate (``wins`` / ``coins``). We add credit for
    proven graduations, an accumulator (not dumper) trade bias, and net SOL
    inflow, then dampen low-sample wallets so one lucky hit can't top the board.
    Pure + unit-tested.
    """
    coins_n = max(1, coins)
    hit_rate = _clamp(wins / coins_n)
    net = sol_in - sol_out
    total = max(1, buys + sells)
    buy_ratio = buys / total
    score = (hit_rate * 48
             + min(1.0, grads / 3.0) * 16            # proven graduations
             + _clamp((buy_ratio - 0.4) / 0.5) * 12  # accumulator vs dumper
             + _clamp(net / 10.0) * 12               # net SOL inflow (10 SOL = full)
             + min(1.0, coins_n / 8.0) * 12)         # breadth / sample size
    score *= _clamp(0.45 + coins_n * 0.18)           # low-sample confidence dampener
    if net < 0:
        score -= 8                                   # net seller penalty
    return round(_clamp(score, 0, 100), 1)


def _short(w: str) -> str:
    return (w[:4] + ".." + w[-4:]) if w and len(w) > 8 else (w or "?")
