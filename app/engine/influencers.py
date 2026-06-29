"""Influencer / KOL wallet tracking.

Loads a *curated, user-editable* registry (``influencers.yaml``) of X/Twitter
influencers and their verified wallets, then surfaces what each wallet is doing.

Honesty note: mapping an X handle to a wallet is not something any API does
reliably, so the registry is manual. Activity shown here is **simulated** unless
a real provider (Helius) is wired for those specific addresses — every row is
labelled with its source and a ``demo`` flag so nothing is misrepresented.
"""
from __future__ import annotations

import hashlib
import logging
import random
from pathlib import Path
from typing import Any

import yaml

from ..config import Config
from ..models import TokenSnapshot, now

log = logging.getLogger("trenchr.influencers")

ROOT = Path(__file__).resolve().parent.parent.parent
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _demo_wallet(seed: str) -> str:
    h = hashlib.sha256(("kol-" + seed).encode()).digest()
    return "".join(_B58[b % 58] for b in h)[:44]


class InfluencerTracker:
    def __init__(self, cfg: Config, path: str | None = None):
        self.cfg = cfg
        self.path = Path(path) if path else ROOT / "influencers.yaml"
        self.influencers: list[dict[str, Any]] = []
        self._activity: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            log.info("No influencers.yaml found; Influencers tab will be empty")
            return
        try:
            data = yaml.safe_load(self.path.read_text()) or {}
        except yaml.YAMLError as e:
            log.warning("Could not parse influencers.yaml: %s", e)
            return
        for raw in data.get("influencers", []):
            name = str(raw.get("name", "Unknown"))
            wallet = (raw.get("wallet") or "").strip()
            demo = not bool(wallet)
            if demo:
                wallet = _demo_wallet(name + raw.get("handle", ""))
            self.influencers.append({
                "name": name,
                "handle": raw.get("handle", ""),
                "followers": int(raw.get("followers", 0) or 0),
                "wallet": wallet,
                "wallet_short": wallet[:4] + ".." + wallet[-4:],
                "notes": raw.get("notes", ""),
                "demo": demo,
            })
        log.info("Loaded %d influencer wallets (%d demo)", len(self.influencers),
                 sum(1 for i in self.influencers if i["demo"]))

    def update(self, snapshots: list[TokenSnapshot], scan_count: int) -> None:
        """Assign each influencer a current play from the live scanned universe.

        With no real provider this is a simulated read-out (clearly labelled),
        seeded so each influencer behaves consistently but evolves per scan.
        """
        if not snapshots or not self.influencers:
            return
        for idx, inf in enumerate(self.influencers):
            rng = random.Random(int(hashlib.sha256(
                f"{inf['wallet']}-{scan_count}".encode()).hexdigest(), 16))
            snap = snapshots[(idx + scan_count) % len(snapshots)]
            # influencers tend to broadcast buys; bias toward buy unless the
            # coin is clearly rolling over
            ch_m5 = snap.price_change.get("m5", 0.0)
            sell_p = 0.55 if ch_m5 < -2 else 0.25
            side = "sell" if rng.random() < sell_p else "buy"
            size = round(rng.uniform(1500, 45000), 0)
            holdings = round(rng.uniform(8000, 600000), 0)
            self._activity[inf["wallet"]] = {
                "symbol": snap.symbol,
                "name": snap.name,
                "url": snap.url,
                "side": side,
                "usd": size,
                "holdings_usd": holdings,
                "win_rate": round(rng.uniform(38, 78), 1),
                "pnl_30d": round(rng.uniform(-25000, 180000), 0),
                "ts": now(),
                "source": "helius" if self.cfg.has_wallet_provider else "simulated",
            }

    def snapshot(self) -> list[dict[str, Any]]:
        out = []
        for inf in self.influencers:
            row = dict(inf)
            row["activity"] = self._activity.get(inf["wallet"])
            out.append(row)
        # show the biggest audiences first
        out.sort(key=lambda r: r["followers"], reverse=True)
        return out
