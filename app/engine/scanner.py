"""The scanner — the heartbeat that ties everything together.

Each cycle it:
  1. discovers trending Solana memecoins (DexScreener boosts)
  2. enriches them with full pair stats
  3. runs the pump/dump detector
  4. layers on wallet intelligence (smart-money flow, coordinated sells)
  5. marks open positions and applies the exit ladder ("sell before the dump")
  6. opens new positions when the strategy gives a high-confidence entry
  7. records signals + an equity-curve point, and publishes a live snapshot

The latest snapshot is held in memory for the API/WebSocket layer to serve.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from ..config import Config
from ..database import Database
from ..data.dexscreener import DexScreenerClient
from ..data.onchain import OnchainSafety
from ..data.pumpportal import PumpPortalIngester
from ..data.wallets import build_wallet_provider
from ..models import DetectionResult, Signal, TokenIntel, TokenSnapshot
from .detector import detect
from .influencers import InfluencerTracker
from .launchpad import LaunchpadEngine
from .paper_trader import PaperTrader
from .strategy import evaluate_entry, evaluate_exit, update_trailing
from .wallet_intel import WalletIntel

log = logging.getLogger("memeradar.scanner")


def cabal_buy_decision(bundle: dict[str, Any], det: Optional[dict[str, Any]],
                       cfg: Config) -> tuple[bool, str]:
    """Should we follow a coordinated cabal pile-in into this coin? Pure.

    ``bundle`` is a row from WalletIntel.bundles(); ``det`` is the coin's
    detection dict (or None if it isn't on the current board).
    """
    if not cfg.cabal_buy_enabled:
        return False, "cabal-buy disabled"
    if int(bundle.get("wallet_count", 0)) < cfg.cabal_buy_min_wallets:
        return False, "too few wallets"
    if float(bundle.get("span_seconds", 1e12)) > cfg.cabal_buy_window_seconds:
        return False, "outside the time window"
    if cfg.cabal_buy_require_cabal and not bundle.get("cabal_id"):
        return False, "not a known cabal"
    if det is None:
        return False, "coin not analysed"
    if float(det.get("safety", 0)) < cfg.cabal_buy_min_safety:
        return False, "safety too low"
    if float(det.get("dump_risk", 100)) > cfg.cabal_buy_max_dump_risk:
        return False, "dump risk too high"
    if det.get("phase") == "dump":
        return False, "coin is dumping"
    return True, "cabal pile-in cleared the quality gate"


class Scanner:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.dex = DexScreenerClient(chain=cfg.chain)
        self.onchain = OnchainSafety(cfg)
        self.wallets = build_wallet_provider(cfg)
        self.intel = WalletIntel(cfg)
        self.influencers = InfluencerTracker(cfg)
        self.trader = PaperTrader(cfg, db)
        # launchpad: live pump.fun firehose + moonshot scoring + spray
        self.pumpportal = PumpPortalIngester(cfg)
        self.launchpad = LaunchpadEngine(cfg, self.dex, self.pumpportal,
                                         self.trader, self._emit)

        self.running = False
        self.paused = not cfg.auto_trade
        self.snap_by_addr: dict[str, TokenSnapshot] = {}  # latest, for manual trades
        self.last_scan_ts: float = 0.0
        self.scan_count: int = 0
        self.last_error: Optional[str] = None
        # latest analysed universe (token -> bundle) for the dashboard
        self.board: list[dict[str, Any]] = []
        self._task: Optional[asyncio.Task] = None
        self._scan_lock = asyncio.Lock()  # serialises loop + manual /scan
        self._on_update: list[Callable[[], None]] = []
        self._on_signal: list[Callable[[Signal], None]] = []
        self._alerted_bundles: set[str] = set()
        self._cabal_bought: set[str] = set()  # coins entered on a cabal pile-in

    @property
    def is_scanning(self) -> bool:
        return self._scan_lock.locked()

    def on_update(self, cb: Callable[[], None]) -> None:
        self._on_update.append(cb)

    def on_signal(self, cb: Callable[[Signal], None]) -> None:
        """Subscribe to signals as they are emitted (e.g. Telegram alerts)."""
        self._on_signal.append(cb)

    def _emit(self, sig: Signal) -> None:
        self.db.insert_signal(sig)
        for cb in self._on_signal:
            try:
                cb(sig)
            except Exception:  # noqa: BLE001 — a bad listener must not break scanning
                pass

    # --- lifecycle -------------------------------------------------------- #
    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self.running = True
        self.pumpportal.start()        # live pump.fun stream (background)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self.running = False
        await self.pumpportal.stop()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.dex.close()
        await self.onchain.close()
        await self.wallets.close()

    async def _loop(self) -> None:
        # seed an initial equity point so the curve starts at the bankroll
        self.trader.record_equity_point()
        while self.running:
            t0 = time.time()
            try:
                await self.scan_once()
                self.last_error = None
            except Exception as e:  # noqa: BLE001 — keep the loop alive
                self.last_error = str(e)
                log.exception("scan failed: %s", e)
            for cb in self._on_update:
                try:
                    cb()
                except Exception:  # noqa: BLE001
                    pass
            elapsed = time.time() - t0
            await asyncio.sleep(max(2.0, self.cfg.scan_interval_seconds - elapsed))

    # --- one scan cycle --------------------------------------------------- #
    async def scan_once(self) -> None:
        # serialise with the background loop and any manual /scan to avoid
        # overlapping scans stacking double trades
        async with self._scan_lock:
            await self._scan_once_impl()

    async def _scan_once_impl(self) -> None:
        addresses = await self.dex.discover_token_addresses(self.cfg.universe_size)

        # always include tokens we currently hold (so we can manage exits)
        for addr in self.trader.positions:
            if addr not in addresses:
                addresses.append(addr)

        snapshots = await self.dex.enrich(addresses)
        snap_by_addr = {s.address: s for s in snapshots}
        self.snap_by_addr = snap_by_addr

        # on-chain rug check (mint/freeze authority) for the whole universe
        try:
            auth = await self.onchain.authorities([s.address for s in snapshots])
            for s in snapshots:
                flags = auth.get(s.address)
                if flags:
                    s.mint_renounced = flags["mint_renounced"]
                    s.freeze_renounced = flags["freeze_renounced"]
        except Exception as e:  # noqa: BLE001 — never let safety checks kill a scan
            log.warning("on-chain safety check failed: %s", e)

        board: list[dict[str, Any]] = []
        for snap in snapshots:
            det = detect(snap, self.cfg)
            fetch = await self.wallets.fetch(snap)
            intel = self.intel.ingest(snap, fetch.events,
                                      fetch.whale_concentration, fetch.source)
            self._raise_token_signals(snap, det, intel)
            board.append({
                "token": snap.to_dict(),
                "detection": det.to_dict(),
                "intel": intel.to_dict(),
            })

        # manage existing positions first (exit before we deploy more capital)
        self._manage_positions(snap_by_addr,
                               {s.address: detect(s, self.cfg) for s in snapshots},
                               board)

        # consider new entries, ranked by opportunity
        if not self.paused:
            self._consider_entries(snap_by_addr, board)

        # rank the board for the dashboard
        board.sort(key=lambda b: b["detection"]["opportunity"], reverse=True)
        self.board = board

        # forward-looking "fresh loadout" detection + per-wallet career curves
        self.intel.compute_loadouts(board)
        self.intel.compute_bundles(board)
        self.intel.record_careers()
        self._emit_bundle_signals()

        # follow coordinated cabal pile-ins into a coin (paper auto-entry)
        if not self.paused:
            self._consider_cabal_buys(snap_by_addr, board)

        # launchpad: score brand-new pump.fun coins + run spray bets, then make
        # those fresh coins buyable from the dashboard
        try:
            await self.launchpad.refresh()
            for mint, lsnap in self.launchpad.snaps.items():
                self.snap_by_addr.setdefault(mint, lsnap)
        except Exception as e:  # noqa: BLE001 — never let the launchpad kill a scan
            log.warning("launchpad refresh failed: %s", e)

        # update influencer wallet activity against the live universe
        self.influencers.update(snapshots, self.scan_count)

        self.trader.record_equity_point()
        self.scan_count += 1
        self.last_scan_ts = time.time()

    def _raise_token_signals(self, snap: TokenSnapshot, det: DetectionResult,
                             intel: TokenIntel) -> None:
        if det.pump_score >= 78 and "volume_surge" in det.flags:
            self._emit(Signal("pump", "warning", snap.address, snap.symbol,
                              f"Pump detected on {snap.symbol}: score {det.pump_score:.0f}, "
                              f"{', '.join(det.reasons[:2])}",
                              meta={"pump_score": det.pump_score, "url": snap.url}))
        if det.dump_risk >= 72:
            self._emit(Signal("dump", "critical", snap.address, snap.symbol,
                              f"Dump risk on {snap.symbol}: {det.dump_risk:.0f} ({det.phase})",
                              meta={"dump_risk": det.dump_risk, "url": snap.url}))
        if intel.confirmed_multi_sell:
            self._emit(Signal("multi_sell", "critical", snap.address, snap.symbol,
                              f"{intel.multi_sell_wallets} smart wallets dumping {snap.symbol} "
                              f"(${intel.multi_sell_usd:,.0f})",
                              meta={"wallets": intel.multi_sell_wallets,
                                    "usd": intel.multi_sell_usd, "url": snap.url}))
        elif intel.smart_inflow_usd >= self.cfg.whale_min_usd and intel.smart_inflow_score > 35:
            self._emit(Signal("whale_buy", "success", snap.address, snap.symbol,
                              f"Smart-money accumulating {snap.symbol} "
                              f"(${intel.smart_inflow_usd:,.0f} net in)",
                              meta={"inflow": intel.smart_inflow_usd, "url": snap.url}))

    def _emit_bundle_signals(self) -> None:
        """Flag coordinated multi-wallet buys (bundling) once per occurrence."""
        active: set[str] = set()
        for b in self.intel.bundles(20):
            active.add(b["address"])
            if b["severity"] in ("critical", "warning") and b["address"] not in self._alerted_bundles:
                self._alerted_bundles.add(b["address"])
                self._emit(Signal("bundle", b["severity"], b["address"], b["symbol"],
                                  f"Bundle on {b['symbol']}: {b['wallet_count']} wallets bought "
                                  f"{b['label']} (${b['total_usd']:,.0f})",
                                  meta={"wallets": b["wallet_count"], "label": b["label"],
                                        "url": b["url"]}))
        self._alerted_bundles &= active  # allow re-alert if it recurs later

    def _manage_positions(self, snaps: dict[str, TokenSnapshot],
                          dets: dict[str, DetectionResult],
                          board: list[dict[str, Any]]) -> None:
        intel_by_addr = {b["token"]["address"]: b["intel"] for b in board}
        for addr in list(self.trader.positions.keys()):
            pos = self.trader.positions[addr]
            if self.trader.is_spray(pos):
                continue  # spray bets run the launchpad's own fast exit ladder
            snap = snaps.get(addr)
            price = snap.price_usd if snap else pos.last_price
            self.trader.mark(addr, price)
            update_trailing(pos, self.cfg)

            det = dets.get(addr)
            intel_d = intel_by_addr.get(addr)
            intel_obj = self._intel_from_dict(intel_d) if intel_d else None
            decision = evaluate_exit(pos, snap, det, intel_obj, self.cfg)
            if decision.exit:
                trade = self.trader.close_position(addr, price, decision.reason)
                if trade:
                    sev = "success" if trade.pnl >= 0 else "warning"
                    self._emit(Signal("exit", sev, addr, pos.symbol,
                                      f"SOLD {pos.symbol} {trade.pnl_pct:+.1f}% "
                                      f"(${trade.pnl:+.0f}) — {decision.reason}",
                                      meta={"pnl": trade.pnl, "reason": decision.reason}))

    def _consider_entries(self, snaps: dict[str, TokenSnapshot],
                          board: list[dict[str, Any]]) -> None:
        candidates = sorted(board, key=lambda b: b["detection"]["opportunity"], reverse=True)
        for b in candidates:
            if not self.trader.can_open():
                break
            addr = b["token"]["address"]
            snap = snaps.get(addr)
            if not snap or self.trader.has_position(addr):
                continue
            det = self._det_from_dict(b["detection"])
            intel = self._intel_from_dict(b["intel"])
            decision = evaluate_entry(snap, det, intel, self.cfg)
            if decision.enter:
                ctx = self._build_entry_context(b, "auto", decision.reason)
                ctx["confidence"] = round(decision.confidence, 0)
                pos = self.trader.open_position(snap, decision.reason, entry_context=ctx)
                if pos:
                    self._emit(Signal("entry", "info", addr, snap.symbol,
                                      f"BOUGHT {snap.symbol} ${pos.entry_value:.0f} "
                                      f"(conf {decision.confidence:.0f}%) — {decision.reason}",
                                      meta={"confidence": decision.confidence,
                                            "url": snap.url}))

    def _consider_cabal_buys(self, snaps: dict[str, TokenSnapshot],
                             board: list[dict[str, Any]]) -> None:
        """Open a paper position when a known cabal coordinates a buy into a coin
        that passes the quality gate. Deduped per active bundle."""
        if not self.cfg.cabal_buy_enabled:
            return
        det_by_addr = {b["token"]["address"]: b["detection"] for b in board}
        board_by_addr = {b["token"]["address"]: b for b in board}
        active: set[str] = set()
        for bundle in self.intel.bundles(30):
            addr = bundle.get("address")
            if not addr:
                continue
            active.add(addr)
            if addr in self._cabal_bought or self.trader.has_position(addr):
                continue
            if not self.trader.can_open():
                break
            snap = snaps.get(addr)
            if not snap:
                continue
            ok, reason = cabal_buy_decision(bundle, det_by_addr.get(addr), self.cfg)
            if not ok:
                continue
            cabal = bundle.get("cabal_id") or "cabal"
            note = (f"{cabal} · {bundle['wallet_count']} wallets bought "
                    f"{bundle.get('label', 'together')}")
            b = board_by_addr.get(addr)
            ctx = self._build_entry_context(b, "cabal", note) if b else {
                "kind": "cabal", "note": note}
            ctx.update({
                "cabal_id": bundle.get("cabal_id"),
                "cabal_wallet_count": bundle.get("wallet_count"),
                "span_seconds": bundle.get("span_seconds"),
                "bundle_label": bundle.get("label"),
                "bundle_severity": bundle.get("severity"),
                "cabal_wallets": [w.get("wallet_short") for w in
                                  bundle.get("wallets", [])][:8],
            })
            pos = self.trader.open_position(snap, note, entry_context=ctx)
            if pos:
                self._cabal_bought.add(addr)
                self._emit(Signal("entry", "success", addr, snap.symbol,
                                  f"CABAL BUY {snap.symbol} ${pos.entry_value:.0f} — {note}",
                                  meta={"cabal": True, "cabal_id": bundle.get("cabal_id"),
                                        "url": snap.url}))
        self._cabal_bought &= active  # allow a fresh entry if the cabal re-piles later

    # --- entry context (the "why we bought" record for the Trades tab) --- #
    @staticmethod
    def _build_entry_context(b: dict[str, Any], kind: str, note: str) -> dict[str, Any]:
        """Snapshot the evidence behind an entry so a closed trade can explain
        itself: scores, market phase, and which flagged wallets were active."""
        det = b.get("detection", {})
        intel = b.get("intel", {})
        tok = b.get("token", {})
        # which named smart wallets were buying this coin at entry
        flagged = []
        for ev in intel.get("recent_events", []):
            if ev.get("side") == "buy" and ev.get("kind") in (
                    "whale", "insider", "smart_money"):
                flagged.append({
                    "wallet": ev.get("wallet"),
                    "wallet_short": ev.get("wallet_short"),
                    "kind": ev.get("kind"),
                    "usd": round(float(ev.get("usd", 0)), 0),
                    "win_rate": round(float(ev.get("win_rate", 0)) * 100, 0),
                })
        flagged.sort(key=lambda w: w["usd"], reverse=True)
        return {
            "kind": kind,
            "note": note,
            "price_at_entry": tok.get("price_usd"),
            "liquidity_usd": tok.get("liquidity_usd"),
            "market_cap": tok.get("market_cap"),
            "age_minutes": tok.get("age_minutes"),
            "pump_score": det.get("pump_score"),
            "dump_risk": det.get("dump_risk"),
            "safety": det.get("safety"),
            "opportunity": det.get("opportunity"),
            "momentum": det.get("momentum"),
            "phase": det.get("phase"),
            "reasons": list(det.get("reasons", []))[:6],
            "flags": list(det.get("flags", []))[:8],
            "smart_inflow_usd": intel.get("smart_inflow_usd"),
            "smart_inflow_score": intel.get("smart_inflow_score"),
            "whale_count": intel.get("whale_count"),
            "insider_count": intel.get("insider_count"),
            "smart_money_count": intel.get("smart_money_count"),
            "whale_concentration": intel.get("whale_concentration"),
            "confirmed_multi_sell": intel.get("confirmed_multi_sell"),
            "multi_sell_wallets": intel.get("multi_sell_wallets"),
            "flagged_wallets": flagged[:8],
        }

    # --- small dict<->object helpers ------------------------------------- #
    @staticmethod
    def _det_from_dict(d: dict[str, Any]) -> DetectionResult:
        return DetectionResult(**{k: d[k] for k in (
            "address", "symbol", "pump_score", "dump_risk", "momentum",
            "safety", "phase", "opportunity", "reasons", "flags")})

    @staticmethod
    def _intel_from_dict(d: dict[str, Any]) -> TokenIntel:
        return TokenIntel(
            address=d["address"], symbol=d["symbol"],
            smart_inflow_usd=d["smart_inflow_usd"],
            smart_inflow_score=d["smart_inflow_score"],
            whale_count=d["whale_count"], insider_count=d["insider_count"],
            smart_money_count=d["smart_money_count"],
            whale_concentration=d["whale_concentration"],
            confirmed_multi_sell=d["confirmed_multi_sell"],
            multi_sell_wallets=d["multi_sell_wallets"],
            multi_sell_usd=d["multi_sell_usd"], source=d["source"],
        )

    # --- manual trading (from the dashboard) ----------------------------- #
    def manual_buy(self, address: str, usd: float) -> dict[str, Any]:
        snap = self.snap_by_addr.get(address)
        if not snap:
            return {"ok": False, "error": "coin not in the current scan universe"}
        if usd <= 0:
            return {"ok": False, "error": "amount must be positive"}
        board_entry = next((b for b in self.board
                            if b["token"]["address"] == address), None)
        ctx = (self._build_entry_context(board_entry, "manual", "manual buy")
               if board_entry else {"kind": "manual", "note": "manual buy"})
        pos = self.trader.manual_buy(snap, usd, entry_context=ctx)
        if not pos:
            return {"ok": False, "error": "buy failed (insufficient cash?)"}
        self._emit(Signal("entry", "info", address, snap.symbol,
                          f"MANUAL BUY {snap.symbol} ${usd:,.0f}",
                          meta={"manual": True, "url": snap.url}))
        return {"ok": True, "symbol": snap.symbol, "cash": round(self.trader.cash, 2)}

    def manual_sell(self, address: str, fraction: float = 1.0) -> dict[str, Any]:
        pos = self.trader.positions.get(address)
        if not pos:
            return {"ok": False, "error": "no open position in that coin"}
        snap = self.snap_by_addr.get(address)
        price = snap.price_usd if snap else pos.last_price
        trade = self.trader.manual_sell(address, price, fraction)
        if not trade:
            return {"ok": False, "error": "sell failed"}
        sev = "success" if trade.pnl >= 0 else "warning"
        self._emit(Signal("exit", sev, address, trade.symbol,
                          f"MANUAL SELL {trade.symbol} {trade.pnl_pct:+.1f}% "
                          f"(${trade.pnl:+.0f})", meta={"manual": True}))
        return {"ok": True, "pnl": trade.pnl, "cash": round(self.trader.cash, 2)}

    # --- snapshot for the API -------------------------------------------- #
    def snapshot(self) -> dict[str, Any]:
        st = self.trader.portfolio_state()
        return {
            "portfolio": st.to_dict(),
            "positions": [p.to_dict() for p in self.trader.positions.values()],
            "board": self.board,
            "status": {
                "running": self.running,
                "paused": self.paused,
                "scan_count": self.scan_count,
                "last_scan_ts": self.last_scan_ts,
                "last_error": self.last_error,
                "chain": self.cfg.chain,
                "wallet_provider": "helius" if self.cfg.has_wallet_provider else "simulated",
                "launchpad": self.pumpportal.connected,
            },
        }

    def launchpad_snapshot(self) -> dict[str, Any]:
        return self.launchpad.snapshot()
