"""On-chain rug safety checks via the Solana RPC.

Research is unambiguous that the large majority of Solana memecoins are rugs or
soft-rugs, and the classic hard tells are an un-renounced **mint authority**
(the dev can print unlimited supply) or **freeze authority** (the dev can freeze
your wallet — a honeypot). Both are readable for **free** from the public Solana
RPC and can be fetched for the whole scan universe in a single batched
``getMultipleAccounts`` call, so this runs with no API key.

Results are cached per mint (authorities effectively only ever get *renounced*,
never re-granted), so each scan only queries newly-seen tokens.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from ..config import Config

log = logging.getLogger("trenchr.onchain")

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"


def _ca_bundle() -> Any:
    return os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or True


class OnchainSafety:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        # prefer Helius (higher limits) when a key exists, else the free RPC
        if cfg.helius_api_key:
            self.rpc = f"https://mainnet.helius-rpc.com/?api-key={cfg.helius_api_key}"
        else:
            self.rpc = os.environ.get("SOLANA_RPC_URL", PUBLIC_RPC)
        self._client = httpx.AsyncClient(trust_env=True, verify=_ca_bundle(), timeout=15.0)
        # mint -> {"mint_renounced": bool, "freeze_renounced": bool}
        self._cache: dict[str, dict[str, bool]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def authorities(self, mints: list[str]) -> dict[str, dict[str, bool]]:
        """Return renounce status for each mint, using the cache where possible."""
        if not self.cfg.safety_check_authority:
            return {}
        todo = [m for m in mints if m and m not in self._cache]
        for i in range(0, len(todo), 100):
            await self._fetch_chunk(todo[i:i + 100])
        return {m: self._cache[m] for m in mints if m in self._cache}

    async def _fetch_chunk(self, mints: list[str]) -> None:
        if not mints:
            return
        try:
            r = await self._client.post(self.rpc, json={
                "jsonrpc": "2.0", "id": "trenchr", "method": "getMultipleAccounts",
                "params": [mints, {"encoding": "jsonParsed"}]})
            r.raise_for_status()
            values = (r.json().get("result") or {}).get("value") or []
        except (httpx.HTTPError, ValueError) as e:
            log.warning("getMultipleAccounts failed (%d mints): %s", len(mints), e)
            return
        for mint, acc in zip(mints, values):
            info = self._parse_info(acc)
            if info is None:
                continue
            self._cache[mint] = {
                "mint_renounced": info.get("mintAuthority") in (None, ""),
                "freeze_renounced": info.get("freezeAuthority") in (None, ""),
            }

    @staticmethod
    def _parse_info(acc: Any) -> dict[str, Any] | None:
        try:
            return acc["data"]["parsed"]["info"]
        except (TypeError, KeyError):
            return None
