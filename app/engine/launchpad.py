"""Launchpad engine — score brand-new pump.fun coins and (optionally) spray them.

Reads the live new-coin stream from the :class:`PumpPortalIngester`, enriches each
fresh coin with DexScreener market data (which covers pump.fun mints within
seconds of launch), and computes a **moonshot score** — how likely a coin is to
run, judged from early traction *without* signs of a dev rug or distribution.

It also feeds creator-traction back to the ingester so the creator-wallet track
records stay current, and — when **spray mode** is on — places tiny capped paper
bets across the strongest fresh candidates and runs their own fast exit ladder
("each either explodes or goes to zero").

The scoring function is pure and unit-tested.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from ..config import Config
from ..data.dexscreener import DexScreenerClient
from ..data.pumpportal import PUMP_TOTAL_SUPPLY, PumpPortalIngester
from ..models import Signal, TokenSnapshot, now

log = logging.getLogger("memeradar.launchpad")

WSOL_MINT = "So11111111111111111111111111111111111111112"


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def effective_liquidity(snap: Optional[TokenSnapshot], coin: dict[str, Any],
                        sol_usd: float) -> float:
    """Real tradeable liquidity for a fresh pump.fun coin.

    DexScreener frequently reports ``liquidity.usd == 0`` for a coin in its first
    minutes (the bonding-curve liquidity isn't indexed yet) even while price and
    volume are live. So we floor it with the bonding-curve SOL reserve from the
    create event (``vSolInBondingCurve``) converted to USD — a coin streaming on
    pump.fun always has a real, tradeable bonding-curve market.
    """
    dex_liq = snap.liquidity_usd if snap else 0.0
    # both sides of the bonding-curve AMM ≈ 2× the SOL reserve in USD
    bc_liq = 2.0 * float(coin.get("sol_reserve", 0.0)) * max(0.0, sol_usd)
    return max(dex_liq, bc_liq)


def moonshot_score(snap: Optional[TokenSnapshot], coin: dict[str, Any],
                   creator: Optional[dict[str, Any]],
                   cfg: Config, sol_usd: float = 150.0) -> dict[str, Any]:
    """Score a fresh coin 0-100 with reasons + flags. Pure.

    ``snap`` is the live DexScreener snapshot (may be None before the pair lists);
    ``coin`` is the PumpPortal create record; ``creator`` is the creator's track
    record (may be None); ``sol_usd`` converts bonding-curve reserves to USD.
    """
    reasons: list[str] = []
    flags: list[str] = []

    cr_hit = (creator or {}).get("hit_rate", 0) / 100.0
    cr_grad = (creator or {}).get("graduated", 0)
    cr_launches = (creator or {}).get("launches", 0)
    has_socials = bool(coin.get("uri"))
    dev_frac = (coin.get("initial_buy", 0.0) / PUMP_TOTAL_SUPPLY) if PUMP_TOTAL_SUPPLY else 0.0

    if snap is None:
        # pre-market: no live price yet — provisional, creator/socials only
        score = 30 + cr_hit * 25 + (8 if has_socials else 0) + min(10, cr_grad * 5)
        if dev_frac > 0.25:
            score -= 12
            flags.append("dev_heavy")
        flags.append("pre_market")
        reasons.append("just launched — waiting for the pair to list")
        if creator and cr_grad:
            reasons.append(f"creator graduated {cr_grad} before")
        return {"score": round(_clamp(score, 0, 100), 1), "reasons": reasons,
                "flags": flags, "phase": "forming", "live": False}

    buys5, sells5 = snap.buys("m5"), snap.sells("m5")
    buys1, sells1 = snap.buys("h1"), snap.sells("h1")
    tot5 = max(1, buys5 + sells5)
    buy_ratio5 = buys5 / tot5
    vol5 = snap.volume.get("m5", 0.0)
    vol1 = snap.volume.get("h1", 0.0)
    liq = effective_liquidity(snap, coin, sol_usd)   # bonding-curve floored
    ch5 = snap.price_change.get("m5", 0.0)
    ch1 = snap.price_change.get("h1", 0.0)
    mcap = snap.market_cap or snap.fdv

    # --- positive components (each 0..1) --------------------------------- #
    velocity = _clamp(min(1.0, buys5 / 25.0) * 0.6 + min(1.0, vol5 / 15000.0) * 0.4)
    buy_pressure = _clamp((buy_ratio5 - 0.4) / 0.5)
    impulse = _clamp(ch5 / 45.0) * 0.6 + _clamp(ch1 / 90.0) * 0.4
    # liquidity tent: rewards a forming market (~$15k), fades once already big
    if liq <= 0:
        liq_health = 0.0
    elif liq < 18000:
        liq_health = _clamp(liq / 18000.0)
    else:
        liq_health = _clamp(1.0 - (liq - 18000.0) / 160000.0, 0.25, 1.0)
    breadth = _clamp(buys1 / 60.0)
    creator_q = _clamp(cr_hit) * (1.0 if cr_launches >= 2 else 0.6)
    socials = 1.0 if has_socials else 0.0

    score = (velocity * 26 + buy_pressure * 20 + impulse * 16 + liq_health * 16
             + breadth * 10 + creator_q * 8 + socials * 4)

    if velocity > 0.5:
        reasons.append(f"{buys5} buys / {_money(vol5)} vol in 5m")
    if buy_ratio5 >= 0.7:
        reasons.append(f"{buy_ratio5*100:.0f}% buys (5m)")
    if ch5 > 12:
        reasons.append(f"+{ch5:.0f}% in 5m")
    elif ch1 > 25:
        reasons.append(f"+{ch1:.0f}% in 1h")
    if liq >= 8000:
        reasons.append(f"{_money(liq)} liquidity forming")
    if creator and (cr_grad or cr_hit > 0.3):
        reasons.append(f"creator hit-rate {(creator or {}).get('hit_rate',0):.0f}%"
                       + (f", graduated {cr_grad}" if cr_grad else ""))

    # --- penalties / flags ----------------------------------------------- #
    if liq < 1500 and vol1 < 500:
        score = min(score, 35)
        flags.append("no_liquidity")
    if ch5 < -8 and sells5 > buys5:
        score -= 18
        flags.append("dumping")
        reasons.append(f"{ch5:.0f}% (5m) on net selling — distribution")
    if dev_frac > 0.25:
        score -= 14
        flags.append("dev_heavy")
        reasons.append(f"dev took {dev_frac*100:.0f}% of supply at launch")
    if cr_launches >= 4 and cr_grad == 0 and (creator or {}).get("tractions", 0) == 0:
        score -= 12
        flags.append("serial_launcher")
        reasons.append(f"creator launched {cr_launches} coins, none took off")
    if mcap and mcap > 400000:
        score -= 8
        flags.append("already_run")
    if coin.get("migrated"):
        score += 6
        reasons.append("already graduated 🎓")
        flags.append("graduated")

    # phase label
    if snap.age_minutes < 2:
        phase = "fresh"
    elif ch5 > 4 and buy_ratio5 > 0.55:
        phase = "running"
    elif ch5 < -6:
        phase = "dumping"
    else:
        phase = "drifting"

    return {"score": round(_clamp(score, 0, 100), 1), "reasons": reasons[:6],
            "flags": flags[:6], "phase": phase, "live": True}


def _money(n: float) -> str:
    n = float(n or 0)
    if n >= 1e6:
        return f"${n/1e6:.1f}M"
    if n >= 1e3:
        return f"${n/1e3:.0f}K"
    return f"${n:.0f}"


class LaunchpadEngine:
    def __init__(self, cfg: Config, dex: DexScreenerClient,
                 ingester: PumpPortalIngester, trader, emit: Callable[[Signal], None]):
        self.cfg = cfg
        self.dex = dex
        self.ingester = ingester
        self.trader = trader
        self._emit = emit
        self.board: list[dict[str, Any]] = []
        self.snaps: dict[str, TokenSnapshot] = {}     # mint -> latest snapshot
        self._sprayed: set[str] = set()                # mints we've already bet on
        self.spray_enabled: bool = cfg.spray_enabled
        self.last_refresh_ts: float = 0.0
        self._sol_usd: float = 150.0                   # refreshed live; floors fresh liquidity
        self._sol_usd_ts: float = 0.0

    # --- main entry point (called once per scanner cycle) ----------------- #
    async def _refresh_sol_price(self) -> None:
        if now() - self._sol_usd_ts < 300:   # cache for 5 min
            return
        try:
            px = await self.dex.price_for(WSOL_MINT)
            if px and px > 0:
                self._sol_usd = px
                self._sol_usd_ts = now()
        except Exception:  # noqa: BLE001 — keep the cached/default price
            pass

    async def refresh(self) -> None:
        if not self.cfg.launchpad_enabled:
            return
        await self._refresh_sol_price()
        coins = self.ingester.recent_coins()
        # newest first, capped
        coins.sort(key=lambda c: c["created_at"], reverse=True)
        coins = coins[: self.cfg.launchpad_universe]
        mints = [c["mint"] for c in coins]

        snap_by_addr: dict[str, TokenSnapshot] = {}
        if mints:
            try:
                snaps = await self.dex.enrich(mints)
                snap_by_addr = {s.address: s for s in snaps}
            except Exception as e:  # noqa: BLE001 — never let enrichment kill the loop
                log.warning("launchpad enrichment failed: %s", e)

        board: list[dict[str, Any]] = []
        for coin in coins:
            mint = coin["mint"]
            snap = snap_by_addr.get(mint)
            if snap is not None:
                self.ingester.note_traction(mint, snap.liquidity_usd,
                                            snap.volume.get("h1", 0.0))
                self.snaps[mint] = snap
            creator = self.ingester.creator_record(coin["creator"])
            ms = moonshot_score(snap, coin, creator, self.cfg, self._sol_usd)
            board.append(self._board_entry(coin, snap, creator, ms))

        board.sort(key=lambda b: b["moonshot"], reverse=True)
        self.board = board
        self.last_refresh_ts = now()

        # spend guard: only subscribe the per-trade (metered) stream to the few
        # highest-scoring fresh coins worth attributing buyers on
        if self.cfg.has_pumpportal_trades:
            watch = [b["mint"] for b in board
                     if b["live"] and b["moonshot"] >= self.cfg.launchpad_trade_min_score
                     and "dumping" not in b["flags"]][: self.cfg.launchpad_trade_watch_max]
            self.ingester.set_trade_watch(watch)

        # manage + open spray bets (paper)
        try:
            self._manage_spray(snap_by_addr)
            if self.spray_enabled:
                self._open_spray(board, snap_by_addr)
        except Exception as e:  # noqa: BLE001
            log.warning("spray step failed: %s", e)

        # prune snapshot cache for coins no longer tracked
        self.snaps = {m: s for m, s in self.snaps.items()
                      if self.ingester.coin(m) is not None}

    def _board_entry(self, coin: dict[str, Any], snap: Optional[TokenSnapshot],
                     creator: Optional[dict[str, Any]], ms: dict[str, Any]) -> dict[str, Any]:
        age_min = max(0.0, (now() - coin["created_at"]) / 60.0)
        held = self.trader.positions.get(coin["mint"])
        eff_liq = effective_liquidity(snap, coin, self._sol_usd) if snap else None
        dex_liq = snap.liquidity_usd if snap else None
        return {
            "mint": coin["mint"],
            "symbol": coin["symbol"],
            "name": coin["name"],
            "creator": coin["creator"],
            "creator_short": _short(coin["creator"]),
            "creator_rec": creator,
            "age_seconds": round(now() - coin["created_at"], 0),
            "age_minutes": round(age_min, 1),
            "uri": coin["uri"],
            "pool": coin["pool"],
            "migrated": coin["migrated"],
            "init_mcap_sol": round(coin["init_mcap_sol"], 2),
            "moonshot": ms["score"],
            "reasons": ms["reasons"],
            "flags": ms["flags"],
            "phase": ms["phase"],
            "live": ms["live"],
            "price_usd": snap.price_usd if snap else None,
            "liquidity_usd": eff_liq,                 # bonding-curve floored
            "dex_liquidity_usd": dex_liq,             # raw DexScreener (often 0 when fresh)
            "bonding_curve_liq": bool(eff_liq and dex_liq is not None and eff_liq > dex_liq),
            "market_cap": (snap.market_cap or snap.fdv) if snap else None,
            "volume_h1": snap.volume.get("h1", 0.0) if snap else None,
            "price_change": snap.price_change if snap else {},
            "url": snap.url if snap else f"https://pump.fun/{coin['mint']}",
            "dex_address": snap.address if snap else coin["mint"],
            "sprayed": coin["mint"] in self._sprayed,
            "held": held is not None,
            "buyers": len(coin.get("buyers", [])),
        }

    # --- spray paper trading --------------------------------------------- #
    def _spray_positions(self) -> dict[str, Any]:
        return {a: p for a, p in self.trader.positions.items()
                if (p.entry_context or {}).get("kind") == "spray"}

    def _manage_spray(self, snap_by_addr: dict[str, TokenSnapshot]) -> None:
        cfg = self.cfg
        for addr, pos in list(self._spray_positions().items()):
            snap = snap_by_addr.get(addr) or self.snaps.get(addr)
            price = snap.price_usd if (snap and snap.price_usd > 0) else pos.last_price
            self.trader.mark(addr, price)
            mult = price / pos.entry_price if pos.entry_price > 0 else 1.0
            hold_min = pos.hold_seconds / 60.0
            # only a *real* (indexed, non-zero) DexScreener liquidity collapse is
            # trustworthy — fresh coins legitimately read 0 before indexing
            dex_liq = snap.liquidity_usd if snap else None

            reason = None
            if mult >= cfg.spray_take_profit_mult:
                reason = f"moonshot +{(mult-1)*100:.0f}% ({mult:.1f}x)"
            elif mult <= (1.0 - cfg.spray_stop_loss_pct):
                reason = f"spray stop {(mult-1)*100:.0f}%"
            elif dex_liq and 0 < dex_liq < cfg.spray_min_liquidity_usd * 0.4:
                reason = "liquidity drained — bail"
            elif hold_min >= cfg.spray_max_hold_minutes:
                reason = f"spray timeout ({hold_min:.0f}m, flat)"
            if reason:
                trade = self.trader.manual_sell(addr, price, 1.0, reason=reason)
                if trade:
                    sev = "success" if trade.pnl >= 0 else "warning"
                    self._emit(Signal("spray", sev, addr, pos.symbol,
                                      f"SPRAY exit {pos.symbol} {trade.pnl_pct:+.0f}% "
                                      f"(${trade.pnl:+.0f}) — {reason}",
                                      meta={"spray": True, "pnl": trade.pnl}))

    def _open_spray(self, board: list[dict[str, Any]],
                    snap_by_addr: dict[str, TokenSnapshot]) -> None:
        cfg = self.cfg
        current = self._spray_positions()
        spray_usd = sum(p.entry_value for p in current.values())
        count = len(current)
        for b in board:
            if count >= cfg.spray_max_positions:
                break
            if spray_usd + cfg.spray_bet_usd > cfg.spray_max_total_usd:
                break
            mint = b["mint"]
            if (b["moonshot"] < cfg.spray_min_score or not b["live"]
                    or mint in self._sprayed or mint in self.trader.positions):
                continue
            if "dumping" in b["flags"] or "no_liquidity" in b["flags"]:
                continue
            snap = snap_by_addr.get(mint)
            coin = self.ingester.coin(mint)
            if not snap or snap.price_usd <= 0:
                continue
            # need an actually-tradeable market to mark & exit: trust observed
            # liquidity OR recent volume (fresh pump coins often read liq≈0 on
            # DexScreener for a few minutes even while trading actively)
            tradeable = max(snap.liquidity_usd, snap.volume.get("h1", 0.0))
            if tradeable < cfg.spray_min_liquidity_usd:
                continue
            eff_liq = effective_liquidity(snap, coin, self._sol_usd) if coin else snap.liquidity_usd
            ctx = {
                "kind": "spray", "note": "spray bet — early moonshot candidate",
                "moonshot": b["moonshot"], "phase": b["phase"],
                "reasons": b["reasons"], "flags": b["flags"],
                "creator": b["creator"], "creator_short": b["creator_short"],
                "creator_hit_rate": (b["creator_rec"] or {}).get("hit_rate"),
                "age_seconds_at_entry": b["age_seconds"],
                "liquidity_usd": eff_liq,
                "market_cap": snap.market_cap or snap.fdv,
            }
            pos = self.trader.spray_buy(snap, cfg.spray_bet_usd, entry_context=ctx)
            if pos:
                self._sprayed.add(mint)
                count += 1
                spray_usd += cfg.spray_bet_usd
                self._emit(Signal("spray", "info", mint, snap.symbol,
                                  f"SPRAY {snap.symbol} ${cfg.spray_bet_usd:.0f} "
                                  f"(moonshot {b['moonshot']:.0f}, {b['age_minutes']:.0f}m old)",
                                  meta={"spray": True, "moonshot": b["moonshot"],
                                        "url": snap.url}))

    def set_spray(self, on: bool) -> bool:
        self.spray_enabled = bool(on)
        log.info("Spray mode %s", "ON" if self.spray_enabled else "OFF")
        return self.spray_enabled

    # --- API views -------------------------------------------------------- #
    def snapshot(self) -> dict[str, Any]:
        spray = self._spray_positions()
        spray_list = [p.to_dict() for p in spray.values()]
        spray_pnl = sum(p.unrealized_pnl for p in spray.values())
        return {
            "board": self.board[:80],
            "stats": {**self.ingester.stats(),
                      "spray_enabled": self.spray_enabled,
                      "spray_open": len(spray),
                      "spray_deployed": round(sum(p.entry_value for p in spray.values()), 2),
                      "spray_unrealized": round(spray_pnl, 2),
                      "surfaced": sum(1 for b in self.board
                                      if b["moonshot"] >= self.cfg.moonshot_min_score),
                      "last_refresh_ts": self.last_refresh_ts},
            "spray_positions": spray_list,
            "discovered": self.ingester.discovered_creators(40),
            "buyers": (self.ingester.discovered_buyers(40)
                       if self.cfg.has_pumpportal_trades else []),
        }


def _short(w: str) -> str:
    return (w[:4] + ".." + w[-4:]) if w and len(w) > 8 else (w or "?")
