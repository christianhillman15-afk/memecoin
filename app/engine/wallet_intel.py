"""Wallet intelligence aggregation.

Turns a stream of :class:`WalletEvent`s into per-token intelligence:

* **smart-money inflow** — net USD that whales / insiders / smart-money are
  putting in (positive) or pulling out (negative).
* **coordinated multi-wallet sell** — the key "only act when you know for sure"
  signal: fires only when >= ``multi_sell_min_wallets`` *distinct* tracked smart
  wallets sell the *same* token inside ``multi_sell_window_seconds`` **and** the
  combined size clears ``multi_sell_min_usd``.

It also maintains a global registry of tracked wallets for the dashboard's
whale/insider watchlist.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Deque

from ..config import Config
from ..models import TokenIntel, TokenSnapshot, WalletEvent, now

SMART_KINDS = {"whale", "insider", "smart_money"}


class WalletIntel:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        # rolling per-token event buffers (timestamped)
        self._events: dict[str, Deque[WalletEvent]] = {}
        # global registry: wallet -> aggregate profile
        self.registry: dict[str, dict[str, Any]] = {}

    def _buffer(self, token: str) -> Deque[WalletEvent]:
        if token not in self._events:
            self._events[token] = deque(maxlen=200)
        return self._events[token]

    def ingest(self, snap: TokenSnapshot, events: list[WalletEvent],
               whale_concentration: float, source: str) -> TokenIntel:
        buf = self._buffer(snap.address)
        for e in events:
            buf.append(e)
            self._update_registry(e)

        window = self.cfg.multi_sell_window_seconds
        cutoff = now() - window
        recent = [e for e in buf if e.ts >= cutoff]

        # net smart-money flow over the window
        inflow = 0.0
        whales, insiders, smart = set(), set(), set()
        for e in recent:
            if e.kind not in SMART_KINDS:
                continue
            inflow += e.usd if e.side == "buy" else -e.usd
            if e.kind == "whale":
                whales.add(e.wallet)
            elif e.kind == "insider":
                insiders.add(e.wallet)
            elif e.kind == "smart_money":
                smart.add(e.wallet)

        gross = sum(e.usd for e in recent if e.kind in SMART_KINDS) or 1.0
        inflow_score = max(-100.0, min(100.0, inflow / gross * 100.0))

        # coordinated sell detection
        sellers: dict[str, float] = {}
        for e in recent:
            if e.side == "sell" and e.kind in SMART_KINDS:
                sellers[e.wallet] = sellers.get(e.wallet, 0.0) + e.usd
        sell_wallets = len(sellers)
        sell_usd = sum(sellers.values())
        confirmed = (sell_wallets >= self.cfg.multi_sell_min_wallets
                     and sell_usd >= self.cfg.multi_sell_min_usd)

        return TokenIntel(
            address=snap.address, symbol=snap.symbol,
            smart_inflow_usd=round(inflow, 2),
            smart_inflow_score=round(inflow_score, 1),
            whale_count=len(whales), insider_count=len(insiders),
            smart_money_count=len(smart),
            whale_concentration=whale_concentration,
            confirmed_multi_sell=confirmed,
            multi_sell_wallets=sell_wallets,
            multi_sell_usd=round(sell_usd, 2),
            source=source,
            recent_events=list(reversed(recent))[:8],
        )

    def _update_registry(self, e: WalletEvent) -> None:
        w = self.registry.get(e.wallet)
        if not w:
            w = {"wallet": e.wallet, "kind": e.kind, "buy_usd": 0.0,
                 "sell_usd": 0.0, "events": 0, "tokens": set(), "last_seen": 0.0}
            self.registry[e.wallet] = w
        # promote "kind" to the most notable label seen
        rank = {"retail": 0, "smart_money": 1, "insider": 2, "whale": 3}
        if rank.get(e.kind, 0) >= rank.get(w["kind"], 0):
            w["kind"] = e.kind
        if e.side == "buy":
            w["buy_usd"] += e.usd
        else:
            w["sell_usd"] += e.usd
        w["events"] += 1
        w["tokens"].add(e.symbol)
        w["last_seen"] = e.ts

    def top_wallets(self, limit: int = 15) -> list[dict[str, Any]]:
        """Most active tracked smart wallets, for the dashboard watchlist."""
        rows = []
        for w in self.registry.values():
            if w["kind"] == "retail":
                continue
            net = w["buy_usd"] - w["sell_usd"]
            rows.append({
                "wallet": w["wallet"],
                "wallet_short": w["wallet"][:4] + ".." + w["wallet"][-4:],
                "kind": w["kind"],
                "net_usd": round(net, 2),
                "buy_usd": round(w["buy_usd"], 2),
                "sell_usd": round(w["sell_usd"], 2),
                "events": w["events"],
                "tokens": sorted(w["tokens"])[:6],
                "last_seen": w["last_seen"],
            })
        rows.sort(key=lambda r: (abs(r["net_usd"]), r["events"]), reverse=True)
        return rows[:limit]
