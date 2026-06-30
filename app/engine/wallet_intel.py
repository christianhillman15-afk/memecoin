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

import hashlib
import random
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

KIND_WEIGHT = {"whale": 4, "insider": 5, "smart_money": 3, "retail": 0}
CAREER_POINT_SECONDS = 6 * 3600   # modelled spacing between career points
CAREER_MAX_POINTS = 220


def _seed(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest(), 16)


class WalletIntel:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        # rolling per-token event buffers (timestamped)
        self._events: dict[str, Deque[WalletEvent]] = {}
        # global registry: wallet -> aggregate profile
        self.registry: dict[str, dict[str, Any]] = {}
        # how many times each pair of wallets has dumped / bought the same token
        self._co_dumps: dict[tuple[str, str], int] = {}
        self._co_buys: dict[tuple[str, str], int] = {}
        # wallets that acted this scan (for career-curve extension)
        self._touched: set[str] = set()
        # forward-looking "fresh loadout" feed, keyed by token address
        self._loadout_feed: dict[str, dict[str, Any]] = {}
        # bundling feed (coordinated multi-wallet buys), keyed by token address
        self._bundle_feed: dict[str, dict[str, Any]] = {}

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

        # coordinated BUY detection — the forward-looking "team loadout" mirror
        buyers: dict[str, float] = {}
        for e in recent:
            if e.side == "buy" and e.kind in SMART_KINDS:
                buyers[e.wallet] = buyers.get(e.wallet, 0.0) + e.usd
        if len(buyers) >= 2 and sum(buyers.values()) >= self.cfg.multi_sell_min_usd:
            self._record_cobuy(list(buyers.keys()), snap.symbol)

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
                 "tokens": set(), "win_rate": e.win_rate, "dump_hits": 0,
                 "load_hits": 0, "first_seen": e.ts, "last_seen": 0.0,
                 "last_buy_ts": 0.0, "last_sell_ts": 0.0,
                 "recent": deque(maxlen=12)}
            w["career"] = self._seed_career(e.wallet, e.win_rate)
            self.registry[e.wallet] = w
        rank = {"retail": 0, "smart_money": 1, "insider": 2, "whale": 3}
        if rank.get(e.kind, 0) >= rank.get(w["kind"], 0):
            w["kind"] = e.kind
        if e.side == "buy":
            w["buy_usd"] += e.usd
            w["buys"] += 1
            w["last_buy_ts"] = e.ts
        else:
            w["sell_usd"] += e.usd
            w["sells"] += 1
            w["last_sell_ts"] = e.ts
        w["events"] += 1
        w["win_rate"] = max(w["win_rate"], e.win_rate)
        w["tokens"].add(e.symbol)
        w["recent"].append(e)
        w["last_seen"] = e.ts
        self._touched.add(e.wallet)

    # ------------------------------------------------------------------ #
    # modelled "career" equity curve (deterministic per wallet, grows over time)
    # ------------------------------------------------------------------ #
    def _seed_career(self, wallet: str, win_rate: float) -> list[float]:
        rng = random.Random(_seed(wallet + "career"))
        equity = rng.uniform(2_000, 60_000)
        trend = (win_rate - 0.5)  # winners drift up, losers down
        career = [equity]
        for _ in range(rng.randint(24, 48)):
            step = rng.uniform(-0.16, 0.16) + trend * 0.13
            equity = max(50.0, equity * (1 + step))
            career.append(round(equity, 2))
        return career

    def record_careers(self) -> None:
        """Append one career point for every wallet that acted this scan."""
        for wallet in self._touched:
            w = self.registry.get(wallet)
            if not w:
                continue
            career = w["career"]
            idx = len(career)
            rng = random.Random(_seed(f"{wallet}:{idx}"))
            trend = (w["win_rate"] - 0.5)
            net = w["buy_usd"] - w["sell_usd"]
            flow = 0.05 if net > 0 else (-0.04 if net < 0 else 0.0)
            step = rng.uniform(-0.1, 0.1) + trend * 0.09 + flow
            career.append(round(max(50.0, career[-1] * (1 + step)), 2))
            if len(career) > CAREER_MAX_POINTS:
                del career[0]
        self._touched.clear()

    # ------------------------------------------------------------------ #
    # cabal / cluster detection (repeated pairwise co-dumping)
    # ------------------------------------------------------------------ #
    def _record_cabal(self, wallets: list[str], symbol: str) -> None:
        for w in wallets:
            entry = self.registry.get(w)
            if entry:
                entry["dump_hits"] += 1
        for a, b in combinations(sorted(set(wallets)), 2):
            self._co_dumps[(a, b)] = self._co_dumps.get((a, b), 0) + 1

    def _record_cobuy(self, wallets: list[str], symbol: str) -> None:
        for w in wallets:
            entry = self.registry.get(w)
            if entry:
                entry["load_hits"] += 1
        for a, b in combinations(sorted(set(wallets)), 2):
            self._co_buys[(a, b)] = self._co_buys.get((a, b), 0) + 1

    def _components(self, pairs: dict[tuple[str, str], int],
                    hits_key: str) -> list[set[str]]:
        """Connected components over wallet pairs that repeatedly co-act.

        An edge requires both a minimum co-occurrence count and high Jaccard
        overlap of the two wallets' activity, so genuine coordinated groups stay
        separate from wallets that merely cross paths a lot. Used for both
        co-dumping (cabals) and co-buying (accumulation groups).
        """
        adj: dict[str, set[str]] = {}
        for (a, b), n in pairs.items():
            if n < CABAL_MIN_CO_DUMPS:
                continue
            ha = self.registry.get(a, {}).get(hits_key, 0)
            hb = self.registry.get(b, {}).get(hits_key, 0)
            union = ha + hb - n
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

    def _cabal_components(self) -> list[set[str]]:
        return self._components(self._co_dumps, "dump_hits")

    def _accum_components(self) -> list[set[str]]:
        return self._components(self._co_buys, "load_hits")

    def _cabal_id_for(self, wallets: set[str]) -> str | None:
        """The accumulation/dump cabal id shared by >=2 of these wallets, if any."""
        for comp in self._accum_components() + self._cabal_components():
            if len(comp & wallets) >= 2:
                return "cabal-" + min(comp)[:6]
        return None

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

    def _worth(self, wallet: str, tokens: list[str]) -> float:
        return round(sum(h["value_usd"]
                         for h in self._modelled_holdings(wallet, tokens)), 2)

    def wallet_list(self) -> list[dict[str, Any]]:
        """All tracked (non-retail) wallets with the fields the Wallets table
        sorts/filters/searches on (worth, buy amount, date added, etc.)."""
        rows = []
        for w in self.registry.values():
            if w["kind"] == "retail":
                continue
            r = self._row(w)
            r["worth_usd"] = self._worth(w["wallet"], sorted(w["tokens"]))
            r["buys"] = w["buys"]
            r["sells"] = w["sells"]
            r["load_hits"] = w["load_hits"]
            r["first_seen"] = w["first_seen"]
            rows.append(r)
        return rows

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

    # ------------------------------------------------------------------ #
    # fresh loadouts (young coins a strong team is accumulating early)
    # ------------------------------------------------------------------ #
    def _best_cabal(self, wallets: set[str]):
        """Return (cabal_id|None, members_present, component) best matching a set."""
        best, best_n, best_id = None, 0, None
        for comp in self._accum_components() + self._cabal_components():
            n = len(comp & wallets)
            if n > best_n:
                best, best_n, best_id = comp, n, "cabal-" + min(comp)[:6]
        return best_id, best_n, best

    def compute_loadouts(self, board: list[dict[str, Any]]) -> None:
        """Score every young coin a strong team is buying; maintain the feed."""
        cfg = self.cfg
        now_ts = now()
        window = cfg.multi_sell_window_seconds
        cutoff = now_ts - window
        for item in board:
            t, d, i = item["token"], item["detection"], item["intel"]
            addr = t["address"]
            flags = d.get("flags", [])
            age = t.get("age_minutes", 0)
            # hard gates
            if (age > cfg.loadout_max_age_minutes
                    or t.get("liquidity_usd", 0) < cfg.min_liquidity_usd
                    or d.get("safety", 0) < cfg.entry_min_safety
                    or "mint_authority" in flags or "freeze_authority" in flags
                    or i.get("smart_inflow_usd", 0) <= 0
                    or i.get("smart_inflow_score", 0) < 25
                    or d.get("phase") not in ("accumulation", "markup", "quiet")):
                continue
            buf = self._events.get(addr)
            if not buf:
                continue
            buyers: dict[str, list[WalletEvent]] = {}
            for e in buf:
                if e.ts >= cutoff and e.side == "buy" and e.kind in SMART_KINDS:
                    buyers.setdefault(e.wallet, []).append(e)
            if len(buyers) < 2:
                continue
            buyer_set = set(buyers)
            avg_wr = sum(max(ev.win_rate for ev in evs)
                         for evs in buyers.values()) / len(buyers)
            cabal_id, members_present, _ = self._best_cabal(buyer_set)
            kw = sum(KIND_WEIGHT.get(evs[0].kind, 0)
                     for evs in buyers.values()) / len(buyers)

            score = 28 * (i["smart_inflow_score"] / 100)
            score += min(24, 8 * len(buyers))
            score += 18 * avg_wr
            score += 22 if members_present >= 2 else (9 if members_present == 1 else 0)
            score += min(8, kw)
            score += max(0.0, min(10.0,
                         (cfg.loadout_max_age_minutes - age) / cfg.loadout_max_age_minutes * 10))
            if i.get("whale_concentration", 0) > 80:
                score -= 15
            if "thin_liquidity" in flags:
                score -= 10
            if "very_new" in flags:
                score -= 8
            score = max(0.0, min(100.0, score))
            if score < cfg.loadout_min_score:
                continue

            conviction = ("heavy" if score >= 80 else
                          "loading" if score >= 65 else "forming")
            team = []
            for wallet, evs in sorted(buyers.items(),
                                      key=lambda kv: max(e.usd for e in kv[1]),
                                      reverse=True)[:8]:
                team.append({"wallet": wallet,
                             "wallet_short": wallet[:4] + ".." + wallet[-4:],
                             "kind": evs[0].kind,
                             "win_rate": round(max(e.win_rate for e in evs) * 100, 1)})
            entry = self._loadout_feed.get(addr) or {"first_detected_ts": now_ts,
                                                     "scans_seen": 0}
            entry.update({
                "symbol": t["symbol"], "name": t.get("name", ""), "address": addr,
                "pair_address": t.get("pair_address", ""), "chain": t.get("chain", ""),
                "url": t.get("url", ""), "age_minutes": round(age, 1),
                "loadout_score": round(score, 1), "conviction": conviction,
                "smart_inflow_usd": i["smart_inflow_usd"],
                "strong_wallet_count": len(buyers), "phase": d.get("phase"),
                "mint_renounced": t.get("mint_renounced"),
                "freeze_renounced": t.get("freeze_renounced"),
                "whale_concentration": i.get("whale_concentration", 0),
                "source": i.get("source", "simulated"),
                "team": team, "cabal_id": cabal_id if members_present >= 2 else None,
                "is_team_loadout": members_present >= 2,
                "last_seen_ts": now_ts,
            })
            entry["scans_seen"] += 1
            self._loadout_feed[addr] = entry

        # expire stale / aged-out loadouts so the feed stays "fresh"
        ttl = cfg.loadout_ttl_seconds
        for a in list(self._loadout_feed):
            e = self._loadout_feed[a]
            if (now_ts - e["last_seen_ts"] > ttl
                    or e.get("age_minutes", 0) > cfg.loadout_max_age_minutes * 1.5):
                del self._loadout_feed[a]

    def loadouts(self, limit: int = 14) -> list[dict[str, Any]]:
        now_ts = now()
        rows = []
        for e in self._loadout_feed.values():
            freshness = 0.5 ** ((now_ts - e["last_seen_ts"]) / 150.0)
            row = dict(e)
            row["freshness"] = round(freshness, 3)
            rows.append(row)
        rows.sort(key=lambda r: (r["freshness"], r["loadout_score"]), reverse=True)
        return rows[:limit]

    # ------------------------------------------------------------------ #
    # profiles
    # ------------------------------------------------------------------ #
    def _career_series(self, career: list[float]) -> list[dict[str, float]]:
        n = len(career)
        end = now()
        return [{"ts": round(end - (n - 1 - idx) * CAREER_POINT_SECONDS, 0),
                 "equity": v} for idx, v in enumerate(career)]

    def _modelled_holdings(self, wallet: str, tokens: list[str]) -> list[dict[str, Any]]:
        rng = random.Random(_seed(wallet + "holdings"))
        picks = tokens[:6] if tokens else [f"BAG{i}" for i in range(rng.randint(1, 3))]
        out = []
        for sym in picks:
            value = round(rng.uniform(800, 90_000), 2)
            out.append({
                "symbol": sym, "value_usd": value,
                "qty": round(value / rng.uniform(0.0001, 5), 2),
                "unrealized_pct": round(rng.uniform(-60, 220), 1),
                "source": "modelled",
            })
        return out

    def _cabal_member_map(self) -> dict[str, set[str]]:
        groups: dict[str, set[str]] = {}
        for comp in self._accum_components() + self._cabal_components():
            cid = "cabal-" + min(comp)[:6]
            groups.setdefault(cid, set()).update(comp)
        return groups

    def wallet_profile(self, address: str) -> dict[str, Any]:
        w = self.registry.get(address)
        if not w:  # any clicked address resolves to a minimal modelled profile
            career = self._seed_career(address, 0.4)
            holdings = self._modelled_holdings(address, [])
            hv = sum(h["value_usd"] for h in holdings)
            net = career[-1] - career[0]
            return {
                "wallet": address,
                "wallet_short": address[:4] + ".." + address[-4:],
                "kind": "unknown", "source": "simulated", "tracked": False,
                "win_rate": round(0.4 * 100, 1), "net_usd": round(net, 2),
                "roi_pct": round(net / career[0] * 100, 1) if career[0] else 0,
                "realized_usd": round(net * 0.6, 2),
                "unrealized_usd": round(net * 0.4, 2),
                "holdings_value_usd": round(hv, 2), "buy_usd": 0, "sell_usd": 0,
                "events": 0, "buys": 0, "sells": 0, "dump_hits": 0, "load_hits": 0,
                "token_count": 0, "first_seen": 0, "last_seen": 0, "cabal_id": None,
                "holdings": holdings, "career": self._career_series(career),
                "recent_events": [],
            }
        career = w["career"]
        net = career[-1] - career[0]
        holdings = self._modelled_holdings(address, sorted(w["tokens"]))
        hv = sum(h["value_usd"] for h in holdings)
        cabal_id, n_present, _ = self._best_cabal({address})
        return {
            "wallet": address,
            "wallet_short": address[:4] + ".." + address[-4:],
            "kind": w["kind"], "source": "simulated", "tracked": True,
            "win_rate": round(w["win_rate"] * 100, 1),
            "net_usd": round(net, 2),
            "roi_pct": round(net / career[0] * 100, 1) if career[0] else 0,
            "realized_usd": round(net * 0.6, 2),
            "unrealized_usd": round(net * 0.4, 2),
            "holdings_value_usd": round(hv, 2),
            "buy_usd": round(w["buy_usd"], 2), "sell_usd": round(w["sell_usd"], 2),
            "events": w["events"], "buys": w["buys"], "sells": w["sells"],
            "dump_hits": w["dump_hits"], "load_hits": w["load_hits"],
            "token_count": len(w["tokens"]),
            "first_seen": w["first_seen"], "last_seen": w["last_seen"],
            "cabal_id": cabal_id if n_present >= 1 else None,
            "holdings": holdings, "career": self._career_series(career),
            "recent_events": [e.to_dict() for e in reversed(w["recent"])],
        }

    def cabal_profile(self, cabal_id: str) -> dict[str, Any] | None:
        members_set = self._cabal_member_map().get(cabal_id)
        if not members_set:
            return None
        members = [self.registry[w] for w in members_set if w in self.registry]
        if not members:
            return None
        net = sum(m["buy_usd"] - m["sell_usd"] for m in members)
        sell_usd = sum(m["sell_usd"] for m in members)
        dump_hits = sum(m["dump_hits"] for m in members)
        avg_wr = sum(m["win_rate"] for m in members) / len(members)
        tokens: set[str] = set()
        for m in members:
            tokens |= m["tokens"]
        loading_now = [self._slim_loadout(e) for e in self.loadouts(30)
                       if set(t["wallet"] for t in e["team"]) & members_set]
        # combined career = element-wise sum of member careers (padded)
        maxlen = max(len(m["career"]) for m in members)
        combined = []
        for idx in range(maxlen):
            combined.append(round(sum(m["career"][min(idx, len(m["career"]) - 1)]
                                      for m in members), 2))
        return {
            "id": cabal_id, "size": len(members), "source": "simulated",
            "members": [self._row(m) for m in
                        sorted(members, key=lambda x: x["buy_usd"] + x["sell_usd"],
                               reverse=True)],
            "combined": {"net_usd": round(net, 2), "win_rate": round(avg_wr * 100, 1),
                         "sell_usd": round(sell_usd, 2), "dump_hits": dump_hits},
            "shared_tokens": sorted(tokens)[:12], "token_count": len(tokens),
            "loading_now": loading_now[:8],
            "career": self._career_series(combined),
        }

    @staticmethod
    def _slim_loadout(e: dict[str, Any]) -> dict[str, Any]:
        return {"symbol": e["symbol"], "address": e["address"],
                "pair_address": e.get("pair_address", ""), "chain": e.get("chain", ""),
                "age_minutes": e["age_minutes"], "loadout_score": e["loadout_score"],
                "smart_inflow_usd": e["smart_inflow_usd"], "url": e.get("url", "")}

    def accumulating_cabals(self, limit: int = 8) -> list[dict[str, Any]]:
        now_ts = now()
        out = []
        for comp in self._accum_components():
            members = [self.registry[w] for w in comp if w in self.registry]
            if len(members) < 2:
                continue
            cid = "cabal-" + min(comp)[:6]
            last = max((m["last_buy_ts"] for m in members), default=0)
            recency = 0.5 ** ((now_ts - last) / 1800.0) if last else 0.0
            loading = [self._slim_loadout(e) for e in self.loadouts(30)
                       if set(t["wallet"] for t in e["team"]) & comp]
            out.append({
                "id": cid, "size": len(members),
                "avg_win_rate": round(sum(m["win_rate"] for m in members) / len(members) * 100, 1),
                "net_buy_usd": round(sum(m["buy_usd"] - m["sell_usd"] for m in members), 2),
                "recency": round(recency, 3),
                "loading_now": loading[:6],
                "hot": recency > 0.6 and bool(loading),
            })
        out.sort(key=lambda c: (c["hot"], c["recency"], len(c["loading_now"])), reverse=True)
        return out[:limit]

    # ------------------------------------------------------------------ #
    # bundling — coordinated multi-wallet buys of the same coin
    # ------------------------------------------------------------------ #
    @staticmethod
    def _max_distinct_window(buys: list[WalletEvent], window: float):
        """Sliding window: the time-contiguous set of buys (within `window`
        seconds) covering the most DISTINCT wallets."""
        best_events: list[WalletEvent] = []
        best_n = 0
        start = 0
        for end in range(len(buys)):
            while buys[end].ts - buys[start].ts > window:
                start += 1
            window_events = buys[start:end + 1]
            n = len({e.wallet for e in window_events})
            if n > best_n:
                best_n, best_events = n, window_events
        return best_events, best_n

    def _classify_bundle(self, buys: list[WalletEvent]):
        # tightest-first. 3+ wallets within a minute is a true simultaneous
        # bundle (strong); 2 within 20m is coordinated; 2 within a day is minor.
        for window, min_n, severity, label in (
                (60, 3, "critical", "synchronized (<1m)"),
                (1200, 2, "warning", "coordinated (<20m)"),
                (86400, 2, "info", "same-day")):
            events, n = self._max_distinct_window(buys, window)
            if n >= min_n:
                return events, severity, label
        return None, None, None

    def compute_bundles(self, board: list[dict[str, Any]]) -> None:
        now_ts = now()
        for item in board:
            t = item["token"]
            addr = t["address"]
            buf = self._events.get(addr)
            if not buf:
                continue
            buys = sorted([e for e in buf if e.side == "buy" and e.kind in SMART_KINDS],
                          key=lambda e: e.ts)
            if len({e.wallet for e in buys}) < 2:
                continue
            events, severity, label = self._classify_bundle(buys)
            if not events:
                continue
            # one row per distinct wallet in the cluster (largest buy)
            by_wallet: dict[str, WalletEvent] = {}
            for e in events:
                if e.wallet not in by_wallet or e.usd > by_wallet[e.wallet].usd:
                    by_wallet[e.wallet] = e
            wallets = [{"wallet": e.wallet,
                        "wallet_short": e.wallet[:4] + ".." + e.wallet[-4:],
                        "kind": e.kind, "usd": round(e.usd, 2), "ts": e.ts}
                       for e in sorted(by_wallet.values(), key=lambda x: x.usd, reverse=True)]
            span = round(events[-1].ts - events[0].ts, 0)
            cabal_id, members_present, _ = self._best_cabal(set(by_wallet))
            entry = self._bundle_feed.get(addr) or {"first_detected_ts": now_ts}
            entry.update({
                "address": addr, "symbol": t["symbol"], "name": t.get("name", ""),
                "url": t.get("url", ""), "pair_address": t.get("pair_address", ""),
                "chain": t.get("chain", ""), "age_minutes": round(t.get("age_minutes", 0), 1),
                "severity": severity, "label": label, "wallet_count": len(wallets),
                "wallets": wallets, "total_usd": round(sum(w["usd"] for w in wallets), 2),
                "span_seconds": span, "phase": item["detection"].get("phase"),
                "pump_score": item["detection"].get("pump_score"),
                "volume_h1": t.get("volume", {}).get("h1", 0),
                "cabal_id": cabal_id if members_present >= 2 else None,
                "last_seen_ts": now_ts,
            })
            self._bundle_feed[addr] = entry

        for a in list(self._bundle_feed):
            if now_ts - self._bundle_feed[a]["last_seen_ts"] > 600:
                del self._bundle_feed[a]

    def bundles(self, limit: int = 20) -> list[dict[str, Any]]:
        rank = {"critical": 0, "warning": 1, "info": 2}
        rows = list(self._bundle_feed.values())
        rows.sort(key=lambda b: (rank.get(b["severity"], 3), -b["last_seen_ts"]))
        return rows[:limit]
