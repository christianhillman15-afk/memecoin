"""Tests for wallet/cabal profiles + the fresh-loadout detector."""
import time

from app.config import Config
from app.engine.wallet_intel import WalletIntel
from app.models import TokenSnapshot, WalletEvent

CFG = Config()


def _snap(addr="FreshMint111", age_min=30, liq=60000):
    return TokenSnapshot(
        address=addr, symbol="FRESH", name="Fresh Coin", chain="solana",
        pair_address="pair", dex="raydium", url="http://x", price_usd=0.001,
        liquidity_usd=liq, fdv=500000, market_cap=500000,
        pair_created_at_ms=int((time.time() - age_min * 60) * 1000),
        price_change={"m5": 2, "h1": 8}, volume={"h1": 40000},
        txns={"h1": {"buys": 40, "sells": 10}},
        mint_renounced=True, freeze_renounced=True,
    )


def _board_item(snap, ti, phase="markup", safety=70):
    return {"token": snap.to_dict(),
            "detection": {"safety": safety, "flags": [], "phase": phase,
                          "pump_score": 72, "dump_risk": 18, "opportunity": 75},
            "intel": ti.to_dict()}


def _smart_buys(addr, n=3, usd=9000.0):
    now = time.time()
    kinds = ["whale", "insider", "smart_money"]
    return [WalletEvent(wallet=f"Wallet{i}xxxxxxxxxxxxxxxxxxxxxxxxxx{i}", token=addr,
                        symbol="FRESH", side="buy", usd=usd, kind=kinds[i % 3],
                        source="simulated", win_rate=0.7, ts=now) for i in range(n)]


def test_loadout_detected_for_young_team_buy():
    intel = WalletIntel(CFG)
    snap = _snap()
    events = _smart_buys(snap.address, n=3)
    ti = intel.ingest(snap, events, whale_concentration=30, source="simulated")
    intel.compute_loadouts([_board_item(snap, ti)])
    los = intel.loadouts()
    assert los, "expected a fresh loadout"
    lo = next(l for l in los if l["symbol"] == "FRESH")
    assert lo["strong_wallet_count"] >= 2
    assert lo["loadout_score"] >= CFG.loadout_min_score
    assert lo["conviction"] in ("forming", "loading", "heavy")


def test_loadout_rejects_old_coin():
    intel = WalletIntel(CFG)
    snap = _snap(age_min=CFG.loadout_max_age_minutes + 500)  # too old
    events = _smart_buys(snap.address, n=3)
    ti = intel.ingest(snap, events, 30, "simulated")
    intel.compute_loadouts([_board_item(snap, ti)])
    assert not intel.loadouts()


def test_loadout_rejects_single_buyer():
    intel = WalletIntel(CFG)
    snap = _snap()
    events = _smart_buys(snap.address, n=1, usd=30000)  # one whale alone
    ti = intel.ingest(snap, events, 30, "simulated")
    intel.compute_loadouts([_board_item(snap, ti)])
    assert not intel.loadouts()


def test_loadout_rejects_live_mint_authority():
    intel = WalletIntel(CFG)
    snap = _snap()
    events = _smart_buys(snap.address, n=3)
    ti = intel.ingest(snap, events, 30, "simulated")
    item = _board_item(snap, ti)
    item["detection"]["flags"] = ["mint_authority"]
    intel.compute_loadouts([item])
    assert not intel.loadouts()


def test_wallet_profile_tracked():
    intel = WalletIntel(CFG)
    snap = _snap()
    events = _smart_buys(snap.address, n=3)
    intel.ingest(snap, events, 30, "simulated")
    intel.record_careers()
    addr = events[0].wallet
    p = intel.wallet_profile(addr)
    assert p["tracked"] is True
    assert p["career"] and "equity" in p["career"][0]
    assert p["holdings"]
    assert p["source"] == "simulated"
    assert "win_rate" in p and "net_usd" in p


def test_wallet_profile_untracked_resolves():
    intel = WalletIntel(CFG)
    p = intel.wallet_profile("SomeRandomAddr1111111111111111111111111111")
    assert p["tracked"] is False
    assert p["career"]          # any clicked address resolves to a modelled profile
    assert p["holdings"]


def test_career_grows_across_scans():
    intel = WalletIntel(CFG)
    snap = _snap()
    intel.ingest(snap, _smart_buys(snap.address, n=2), 30, "simulated")
    intel.record_careers()
    addr = "Wallet0xxxxxxxxxxxxxxxxxxxxxxxxxx0"
    n1 = len(intel.wallet_profile(addr)["career"])
    intel.ingest(snap, _smart_buys(snap.address, n=2), 30, "simulated")
    intel.record_careers()
    n2 = len(intel.wallet_profile(addr)["career"])
    assert n2 == n1 + 1  # exactly one point added per scan the wallet acted
