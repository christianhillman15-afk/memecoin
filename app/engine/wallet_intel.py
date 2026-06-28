"""Wallet intelligence aggregation.

Turns a stream of :class:`WalletEvent`s into:

* per-token intelligence (smart-money inflow, coordinated multi-wallet sells),
* a global registry of tracked wallets with behavioural stats, and
* categorised wallet lists for the dashboard: **whales**, **insiders**,
  **smart money**, **pump-and-dump actors**, and **cabals** (groups of wallets
  that repeatedly dump the same tokens together, found via union-find over
  confirmed coordinated sells).

The coordinated-sell signal (the "only act when you know for sure" rule) fires
only when >= ``multi_sell_min_wallets`` distinct smart wallets sell the same
token inside ``multi_sell_window_seconds`` AND the combined size clears
``multi_sell_min_usd``.
"""
from __future__ import annotations

from collections import deque
from itertools import combinations
from typing import Any, Deque

from ..config import Config
from ..models import TokenIntel, TokenSnapshot, WalletEvent, now

SMART_KINDS = {"whale", "insider", "smart_money"}
# a pair of wallets must dump together at least this many times to be linked...
CABAL_MIN_CO_DUMPS = 3
# ...and their dump-token sets must overlap strongly (Jaccard), so that genuine
# coordinated groups stay separate from wallets that merely cross paths a lot.
CABAL_MIN_JACCARD = 0.5


class WalletIntel:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        # rolling per-token event buffers (timestamped)
        self._events: dict[str, Deque[WalletEvent]] = {}
        # global registry: wallet -> aggregate profile
        self.registry: dict[str, dict[str, Any]] = {}
        # how many times each pair of wallets has dumped the same token together
        self._co_dumps: dict[tuple[str, str], int] = {}

    # ------------------------------------------------------------------ #
    # ingestion
    # ------------------------------------------------------------------ #
    def _buffer(self, token: str) -> Deque[WalletEvent]:
        if token not in self._events:
            self._events[token] = deque(maxlen=240)
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

        if confirmed:
            self._record_cabal(list(sellers.keys()), snap.symbol)

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
                 "sell_usd": 0.0, "buys": 0, "sells": 0, "events": 0,
                 "tokens": set(), "win_rate": 0.0, "dump_hits": 0,
                 "first_seen": e.ts, "last_seen": 0.0}
            self.registry[e.wallet] = w
        rank = {"retail": 0, "smart_money": 1, "insider": 2, "whale": 3}
        if rank.get(e.kind, 0) >= rank.get(w["kind"], 0):
            w["kind"] = e.kind
        if e.side == "buy":
            w["buy_usd"] += e.usd
            w["buys"] += 1
        else:
            w["sell_usd"] += e.usd
            w["sells"] += 1
        w["events"] += 1
        w["win_rate"] = max(w["win_rate"], e.win_rate)
        w["tokens"].add(e.symbol)
        w["last_seen"] = e.ts

    # ------------------------------------------------------------------ #
    # cabal / cluster detection (repeated pairwise co-dumping)
    # ------------------------------------------------------------------ #
    def _record_cabal(self, wallets: list[str], symbol: str) -> None:
        for w in wallets:
            entry = self.registry.get(w)
            if entry:
                entry["dump_hits"] += 1
        # count every co-selling pair; only pairs that recur become a cabal edge
        for a, b in combinations(sorted(set(wallets)), 2):
            self._co_dumps[(a, b)] = self._co_dumps.get((a, b), 0) + 1

    def _cabal_components(self) -> list[set[str]]:
        """Connected components over wallet pairs that repeatedly co-dump.

        An edge requires both a minimum co-dump count and high *containment*
        (they co-dump in most of the rarer wallet's dumps), which keeps genuine
        coordinated groups separate from wallets that merely overlap often.
        """
        adj: dict[str, set[str]] = {}
        for (a, b), n in self._co_dumps.items():
            if n < CABAL_MIN_CO_DUMPS:
                continue
            da = self.registry.get(a, {}).get("dump_hits", 0)
            db = self.registry.get(b, {}).get("dump_hits", 0)
            union = da + db - n
            if union <= 0 or n / union < CABAL_MIN_JACCARD:
                continue
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
        seen: set[str] = set()
        components: list[set[str]] = []
        for node in adj:
            if node in seen:
                continue
            stack, comp = [node], set()
            while stack:
                cur = stack.pop()
                if cur in seen:
                    continue
                seen.add(cur)
                comp.add(cur)
                stack.extend(adj[cur] - seen)
            components.append(comp)
        return components

    # ------------------------------------------------------------------ #
    # views for the dashboard
    # ------------------------------------------------------------------ #
    def _row(self, w: dict[str, Any]) -> dict[str, Any]:
        net = w["buy_usd"] - w["sell_usd"]
        return {
            "wallet": w["wallet"],
            "wallet_short": w["wallet"][:4] + ".." + w["wallet"][-4:],
            "kind": w["kind"],
            "win_rate": round(w["win_rate"] * 100, 1),
            "net_usd": round(net, 2),
            "buy_usd": round(w["buy_usd"], 2),
            "sell_usd": round(w["sell_usd"], 2),
            "events": w["events"],
            "dump_hits": w["dump_hits"],
            "tokens": sorted(w["tokens"])[:8],
            "token_count": len(w["tokens"]),
            "last_seen": w["last_seen"],
        }

    def top_wallets(self, limit: int = 15) -> list[dict[str, Any]]:
        """Most active tracked smart wallets, for the overview watchlist."""
        rows = [self._row(w) for w in self.registry.values() if w["kind"] != "retail"]
        rows.sort(key=lambda r: (abs(r["net_usd"]), r["events"]), reverse=True)
        return rows[:limit]

    def _is_pump_dumper(self, w: dict[str, Any]) -> bool:
        if w["kind"] == "retail":
            return False
        if w["dump_hits"] >= 2:
            return True
        return (w["events"] >= 4 and w["sells"] > w["buys"]
                and w["sell_usd"] > w["buy_usd"] * 1.3)

    def categorized(self, per_cat: int = 12) -> dict[str, Any]:
        vals = list(self.registry.values())

        whales = [self._row(w) for w in vals if w["kind"] == "whale"]
        whales.sort(key=lambda r: r["buy_usd"] + r["sell_usd"], reverse=True)

        insiders = [self._row(w) for w in vals if w["kind"] == "insider"]
        insiders.sort(key=lambda r: (r["token_count"], abs(r["net_usd"])), reverse=True)

        smart = [self._row(w) for w in vals
                 if w["win_rate"] >= self.cfg.smart_money_min_winrate
                 and (w["buy_usd"] - w["sell_usd"]) > 0 and w["kind"] != "retail"]
        smart.sort(key=lambda r: (r["win_rate"], r["net_usd"]), reverse=True)

        dumpers = [self._row(w) for w in vals if self._is_pump_dumper(w)]
        dumpers.sort(key=lambda r: (r["dump_hits"], r["sell_usd"]), reverse=True)

        return {
            "whales": whales[:per_cat],
            "insiders": insiders[:per_cat],
            "smart_money": smart[:per_cat],
            "pump_dumpers": dumpers[:per_cat],
            "cabals": self._cabals(),
            "counts": {
                "whales": len(whales), "insiders": len(insiders),
                "smart_money": len(smart), "pump_dumpers": len(dumpers),
                "tracked": len([w for w in vals if w["kind"] != "retail"]),
            },
        }

    def _cabals(self, min_size: int = 3, limit: int = 8) -> list[dict[str, Any]]:
        cabals = []
        for comp in self._cabal_components():
            members = [self.registry[w] for w in comp if w in self.registry]
            if len(members) < min_size:
                continue
            tokens: set[str] = set()
            sell_usd = 0.0
            dump_hits = 0
            for m in members:
                tokens |= m["tokens"]
                sell_usd += m["sell_usd"]
                dump_hits += m["dump_hits"]
            root = min(comp)
            cabals.append({
                "id": "cabal-" + root[:6],
                "size": len(members),
                "members": [self._row(m) for m in
                            sorted(members, key=lambda x: x["sell_usd"], reverse=True)[:10]],
                "shared_tokens": sorted(tokens)[:10],
                "token_count": len(tokens),
                "sell_usd": round(sell_usd, 2),
                "dump_hits": dump_hits,
            })
        cabals.sort(key=lambda c: (c["dump_hits"], c["sell_usd"]), reverse=True)
        return cabals[:limit]
