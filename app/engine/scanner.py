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
from ..data.wallets import build_wallet_provider
from ..models import DetectionResult, Signal, TokenIntel, TokenSnapshot
from .detector import detect
from .influencers import InfluencerTracker
from .paper_trader import PaperTrader
from .strategy import evaluate_entry, evaluate_exit, update_trailing
from .wallet_intel import WalletIntel

log = logging.getLogger("memeradar.scanner")


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

        self.running = False
        self.paused = not cfg.auto_trade
        self.last_scan_ts: float = 0.0
        self.scan_count: int = 0
        self.last_error: Optional[str] = None
        # latest analysed universe (token -> bundle) for the dashboard
        self.board: list[dict[str, Any]] = []
        self._task: Optional[asyncio.Task] = None
        self._on_update: list[Callable[[], None]] = []

    def on_update(self, cb: Callable[[], None]) -> None:
        self._on_update.append(cb)

    def _emit(self, sig: Signal) -> None:
        self.db.insert_signal(sig)

    # --- lifecycle -------------------------------------------------------- #
    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self.running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self.running = False
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
        addresses = await self.dex.discover_token_addresses(self.cfg.universe_size)

        # always include tokens we currently hold (so we can manage exits)
        for addr in self.trader.positions:
            if addr not in addresses:
                addresses.append(addr)

        snapshots = await self.dex.enrich(addresses)
        snap_by_addr = {s.address: s for s in snapshots}

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

    def _manage_positions(self, snaps: dict[str, TokenSnapshot],
                          dets: dict[str, DetectionResult],
                          board: list[dict[str, Any]]) -> None:
        intel_by_addr = {b["token"]["address"]: b["intel"] for b in board}
        for addr in list(self.trader.positions.keys()):
            pos = self.trader.positions[addr]
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
                pos = self.trader.open_position(snap, decision.reason)
                if pos:
                    self._emit(Signal("entry", "info", addr, snap.symbol,
                                      f"BOUGHT {snap.symbol} ${pos.entry_value:.0f} "
                                      f"(conf {decision.confidence:.0f}%) — {decision.reason}",
                                      meta={"confidence": decision.confidence,
                                            "url": snap.url}))

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
            },
        }
