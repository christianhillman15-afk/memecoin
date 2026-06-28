"""Async DexScreener client.

DexScreener is free and key-less. We use the token-boosts feeds for trending
memecoin discovery, then enrich each token with full pair stats (price, volume,
txns, liquidity) which power the pump/dump detector.

Docs: https://docs.dexscreener.com/api/reference
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Iterable

import httpx

from ..models import TokenSnapshot

log = logging.getLogger("memeradar.dexscreener")

BASE = "https://api.dexscreener.com"


def _ca_bundle() -> Any:
    # Respect a custom CA (e.g. corporate proxy) without weakening verification.
    return os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or True


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class DexScreenerClient:
    def __init__(self, chain: str = "solana", timeout: float = 15.0):
        self.chain = chain
        self._client = httpx.AsyncClient(
            trust_env=True,
            verify=_ca_bundle(),
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "MemeRadar/1.0"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str) -> Any:
        try:
            r = await self._client.get(f"{BASE}{path}")
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("DexScreener GET %s failed: %s", path, e)
            return None

    # --- discovery -------------------------------------------------------- #
    async def discover_token_addresses(self, limit: int = 45) -> list[str]:
        """Discover memecoins to scan: trending (boosts) + freshly-listed
        (latest token profiles), filtered to our chain. The fresh feed feeds
        young coins to the loadout detector."""
        top, boosted, fresh = await asyncio.gather(
            self._get("/token-boosts/top/v1"),
            self._get("/token-boosts/latest/v1"),
            self._get("/token-profiles/latest/v1"),
        )
        seen: list[str] = []
        for feed in (top or [], boosted or [], fresh or []):
            for item in feed:
                if item.get("chainId") != self.chain:
                    continue
                addr = item.get("tokenAddress")
                if addr and addr not in seen:
                    seen.append(addr)
        return seen[:limit]

    # --- enrichment ------------------------------------------------------- #
    async def enrich(self, addresses: Iterable[str]) -> list[TokenSnapshot]:
        """Resolve each token to its most-liquid pair snapshot.

        The tokens endpoint accepts up to 30 comma-separated addresses.
        """
        addrs = list(addresses)
        snapshots: list[TokenSnapshot] = []
        for i in range(0, len(addrs), 30):
            chunk = addrs[i:i + 30]
            data = await self._get(f"/latest/dex/tokens/{','.join(chunk)}")
            pairs = (data or {}).get("pairs") or []
            best: dict[str, dict[str, Any]] = {}
            for p in pairs:
                if p.get("chainId") != self.chain:
                    continue
                base = (p.get("baseToken") or {}).get("address")
                if not base:
                    continue
                liq = _f((p.get("liquidity") or {}).get("usd"))
                if base not in best or liq > _f((best[base].get("liquidity") or {}).get("usd")):
                    best[base] = p
            for base, p in best.items():
                snapshots.append(self._to_snapshot(p))
        return snapshots

    def _to_snapshot(self, p: dict[str, Any]) -> TokenSnapshot:
        base = p.get("baseToken") or {}
        return TokenSnapshot(
            address=base.get("address", ""),
            symbol=base.get("symbol", "?"),
            name=base.get("name", base.get("symbol", "?")),
            chain=p.get("chainId", self.chain),
            pair_address=p.get("pairAddress", ""),
            dex=p.get("dexId", ""),
            url=p.get("url", ""),
            price_usd=_f(p.get("priceUsd")),
            liquidity_usd=_f((p.get("liquidity") or {}).get("usd")),
            fdv=_f(p.get("fdv")),
            market_cap=_f(p.get("marketCap")),
            pair_created_at_ms=int(p.get("pairCreatedAt") or 0),
            price_change={k: _f(v) for k, v in (p.get("priceChange") or {}).items()},
            volume={k: _f(v) for k, v in (p.get("volume") or {}).items()},
            txns={
                k: {"buys": int((v or {}).get("buys", 0)),
                    "sells": int((v or {}).get("sells", 0))}
                for k, v in (p.get("txns") or {}).items()
            },
        )

    async def price_for(self, address: str) -> float | None:
        """Fetch the latest price for a single token (used to mark positions)."""
        data = await self._get(f"/latest/dex/tokens/{address}")
        pairs = (data or {}).get("pairs") or []
        best_price, best_liq = None, -1.0
        for p in pairs:
            if p.get("chainId") != self.chain:
                continue
            liq = _f((p.get("liquidity") or {}).get("usd"))
            if liq > best_liq:
                best_liq = liq
                best_price = _f(p.get("priceUsd"))
        return best_price
