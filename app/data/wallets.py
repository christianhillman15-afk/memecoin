"""Wallet intelligence providers.

Two providers implement the same interface:

* ``SimulatedWalletProvider`` (default, no API key) — synthesises a *stable*
  roster of whale / insider / smart-money / retail wallets per token and emits
  buy/sell events whose direction and size are driven by the token's **real**
  DexScreener transaction + volume data. This makes the dashboard fully alive
  and the coordinated-sell logic demonstrable without paid infrastructure.

* ``HeliusWalletProvider`` (set ``HELIUS_API_KEY``) — pulls **real** top-holder
  concentration from the Solana RPC (`getTokenLargestAccounts` + `getTokenSupply`)
  so whale-concentration and whale identification reflect on-chain truth. Live
  per-swap attribution requires a streaming indexer and is out of scope for v1,
  so event synthesis still uses the simulated roster (clearly sourced).

Every event/metric carries a ``source`` field so the UI never misrepresents
simulated data as on-chain fact.
"""
from __future__ import annotations

import hashlib
import logging
import os
import random
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Config
from ..models import TokenSnapshot, WalletEvent, now

log = logging.getLogger("memeradar.wallets")

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _ca_bundle() -> Any:
    return os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or True


def _fake_wallet(seed: str) -> str:
    """Deterministic, base58-looking pseudo address from a seed."""
    h = hashlib.sha256(seed.encode()).digest()
    return "".join(_B58[b % 58] for b in h)[:44]


@dataclass
class WalletFetch:
    events: list[WalletEvent] = field(default_factory=list)
    whale_concentration: float = 0.0   # 0-100, top-holder dominance
    holder_count_hint: int = 0
    source: str = "simulated"


@dataclass
class _Wallet:
    address: str
    kind: str          # whale | insider | smart_money | retail
    win_rate: float    # historical hit-rate (used to label smart money)
    entry_rank: int    # how early it entered (lower = more insider-like)
    dumper: bool = False  # behaves as a coordinated pump-and-dump distributor
    cabal: int = -1    # membership in a recurring coordinated group (-1 = none)


class SimulatedWalletProvider:
    """Synthesises wallet activity anchored to real market data.

    A small *syndicate* of wallets recurs across many tokens (like real cabals
    and serial snipers do), so cross-token intelligence — repeat pump-and-dump
    actors and coordinated groups — actually emerges. Each token also has its
    own one-off whales and retail. Everything is clearly sourced "simulated".
    """

    name = "simulated"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._rosters: dict[str, list[_Wallet]] = {}
        self._cabals = self._build_cabals()           # list[list[_Wallet]]
        self._independents = self._build_independents()

    async def close(self) -> None:  # symmetry with the real provider
        return None

    def _build_cabals(self) -> list[list[_Wallet]]:
        """A few fixed coordinated groups (cabals) of recurring dumper wallets.

        Cabal members repeatedly trade the *same* tokens together, which is what
        lets the intel engine rediscover them as distinct clusters downstream.
        """
        rng = random.Random(8675309)
        cabals: list[list[_Wallet]] = []
        specs = [("whale", "whale", "insider", "whale"),
                 ("insider", "whale", "smart_money"),
                 ("whale", "insider", "insider")]
        for ci, kinds in enumerate(specs):
            group = []
            for wi, kind in enumerate(kinds):
                group.append(_Wallet(_fake_wallet(f"cabal-{ci}-{wi}"), kind,
                                     rng.uniform(0.5, 0.82), rng.randint(1, 10),
                                     dumper=True, cabal=ci))
            cabals.append(group)
        return cabals

    def _build_independents(self) -> list[_Wallet]:
        """Recurring but unaffiliated whales / smart money / insiders.

        These are NOT coordinated dumpers — they accumulate and trade on their
        own — so they don't get folded into the cabal clusters.
        """
        rng = random.Random(192837)
        pool: list[_Wallet] = []
        for i in range(3):
            pool.append(_Wallet(_fake_wallet(f"indep-whale-{i}"), "whale",
                                rng.uniform(0.42, 0.68), rng.randint(1, 14)))
        for i in range(4):
            pool.append(_Wallet(_fake_wallet(f"indep-smart-{i}"), "smart_money",
                                rng.uniform(self.cfg.smart_money_min_winrate, 0.93),
                                rng.randint(1, 40)))
        for i in range(2):
            pool.append(_Wallet(_fake_wallet(f"indep-insider-{i}"), "insider",
                                rng.uniform(0.55, 0.85), rng.randint(1, 8)))
        return pool

    def _roster(self, snap: TokenSnapshot) -> list[_Wallet]:
        """A stable cast of wallets for a token (recurs across scans)."""
        if snap.address in self._rosters:
            return self._rosters[snap.address]
        rng = random.Random(int(hashlib.sha256(snap.address.encode()).hexdigest(), 16))
        roster: list[_Wallet] = []
        # ~60% of tokens are "controlled" by a single cabal whose members all
        # pile in together (and later dump together) — this is what forms a
        # detectable coordinated group across many tokens.
        controlling = rng.choice([0, 1, 2, None, None])
        if controlling is not None:
            roster.extend(self._cabals[controlling])
        # a sprinkle of unaffiliated recurring actors (kept light so they don't
        # blur the coordinated groups)
        roster.extend(rng.sample(self._independents, rng.randint(1, 2)))
        # a few token-unique whales
        for i in range(rng.randint(0, 2)):
            roster.append(_Wallet(_fake_wallet(f"{snap.address}-whale-{i}"),
                                  "whale", rng.uniform(0.4, 0.7), rng.randint(1, 15),
                                  dumper=rng.random() < 0.4))
        # retail noise
        for i in range(rng.randint(6, 12)):
            roster.append(_Wallet(_fake_wallet(f"{snap.address}-retail-{i}"),
                                  "retail", rng.uniform(0.2, 0.5), rng.randint(50, 400)))
        self._rosters[snap.address] = roster
        return roster

    async def fetch(self, snap: TokenSnapshot) -> WalletFetch:
        roster = self._roster(snap)
        rng = random.Random()  # per-scan jitter

        buys_h1 = snap.buys("h1")
        sells_h1 = snap.sells("h1")
        total = max(1, buys_h1 + sells_h1)
        # net pressure in [-1, 1]: positive = buy dominated
        pressure = (buys_h1 - sells_h1) / total
        # bias selling when short-term price is rolling over after a run
        ch_m5 = snap.price_change.get("m5", 0.0)
        ch_h1 = snap.price_change.get("h1", 0.0)
        ch_h6 = snap.price_change.get("h6", 0.0)
        distribution = ch_h6 > 25 and (ch_m5 < 0 or ch_h1 < 0)

        avg_trade = (snap.volume.get("h1", 0.0) / total) if total else 250.0
        avg_trade = max(120.0, min(avg_trade, 40000.0))

        events: list[WalletEvent] = []
        # how many notable wallet events to emit this scan (scaled to activity)
        n_events = min(8, max(2, total // 10))
        actors = [w for w in roster if w.kind != "retail"]
        rng.shuffle(actors)
        for w in actors[:n_events]:
            # probability this actor sells vs buys
            sell_p = 0.5 - 0.45 * pressure
            if distribution:
                sell_p = min(0.92, sell_p + 0.35)
            if w.dumper and distribution:
                sell_p = min(0.95, sell_p + 0.2)   # dumpers distribute hard
            if w.kind == "smart_money" and not w.dumper and not distribution and pressure > 0:
                sell_p *= 0.5  # smart money accumulates into strength
            side = "sell" if rng.random() < sell_p else "buy"
            size_mult = {"whale": rng.uniform(6, 22),
                         "insider": rng.uniform(3, 10),
                         "smart_money": rng.uniform(4, 14)}.get(w.kind, 1.0)
            usd = round(avg_trade * size_mult, 2)
            # spread timestamps over the recent window so coordinated-buy
            # ("bundling") timing tiers are realistic, not all simultaneous
            ts = now() - rng.uniform(0, 180)
            events.append(WalletEvent(wallet=w.address, token=snap.address,
                                      symbol=snap.symbol, side=side, usd=usd,
                                      kind=w.kind, source="simulated",
                                      win_rate=round(w.win_rate, 3), ts=ts))

        whale_conc = self._concentration_heuristic(snap)
        return WalletFetch(events=events, whale_concentration=whale_conc,
                           holder_count_hint=int(total * 3), source="simulated")

    def _concentration_heuristic(self, snap: TokenSnapshot) -> float:
        """Estimate top-holder dominance from market structure (0-100)."""
        rng = random.Random(int(hashlib.sha256((snap.address + "conc").encode()).hexdigest(), 16))
        base = rng.uniform(18, 55)
        # thin liquidity vs FDV implies a few wallets hold most of the supply
        if snap.fdv and snap.liquidity_usd:
            ratio = snap.liquidity_usd / snap.fdv
            if ratio < 0.02:
                base += 25
            elif ratio < 0.05:
                base += 12
        return round(min(95.0, base), 1)


class HeliusWalletProvider:
    """Real holder concentration via Solana RPC; simulated event synthesis."""

    name = "helius"

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._sim = SimulatedWalletProvider(cfg)
        self._rpc = f"https://mainnet.helius-rpc.com/?api-key={cfg.helius_api_key}"
        self._client = httpx.AsyncClient(trust_env=True, verify=_ca_bundle(), timeout=15.0)

    async def close(self) -> None:
        await self._client.aclose()
        await self._sim.close()

    async def _rpc_call(self, method: str, params: list[Any]) -> Any:
        try:
            r = await self._client.post(self._rpc, json={
                "jsonrpc": "2.0", "id": "memeradar", "method": method, "params": params})
            r.raise_for_status()
            return r.json().get("result")
        except (httpx.HTTPError, ValueError) as e:
            log.warning("Helius %s failed: %s", method, e)
            return None

    async def _real_concentration(self, mint: str) -> float | None:
        largest = await self._rpc_call("getTokenLargestAccounts", [mint])
        supply = await self._rpc_call("getTokenSupply", [mint])
        if not largest or not supply:
            return None
        try:
            total = float(supply["value"]["uiAmount"] or 0)
            top10 = sum(float(a["uiAmount"] or 0) for a in largest["value"][:10])
            if total <= 0:
                return None
            return round(min(100.0, top10 / total * 100.0), 1)
        except (KeyError, TypeError, ValueError):
            return None

    async def fetch(self, snap: TokenSnapshot) -> WalletFetch:
        base = await self._sim.fetch(snap)
        conc = await self._real_concentration(snap.address)
        if conc is not None:
            base.whale_concentration = conc
            base.source = "helius+sim"  # concentration real, events synthesised
        return base


def build_wallet_provider(cfg: Config):
    if cfg.has_wallet_provider:
        log.info("Wallet intelligence: Helius (real holder data + synthesised events)")
        return HeliusWalletProvider(cfg)
    log.info("Wallet intelligence: simulated (no HELIUS_API_KEY set)")
    return SimulatedWalletProvider(cfg)
