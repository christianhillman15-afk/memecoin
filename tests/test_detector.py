"""Detector unit tests — pure scoring, no network."""
import time

from app.config import Config
from app.engine.detector import detect
from app.models import TokenSnapshot

CFG = Config()


def _snap(**kw) -> TokenSnapshot:
    now_ms = int(time.time() * 1000)
    base = dict(
        address="MintAddr1111111111111111111111111111111111",
        symbol="PEPE", name="Pepe", chain="solana",
        pair_address="Pair", dex="raydium", url="http://x",
        price_usd=0.001, liquidity_usd=120000, fdv=2_000_000,
        market_cap=2_000_000,
        pair_created_at_ms=now_ms - 3 * 3600 * 1000,  # 3h old
        price_change={"m5": 0, "h1": 0, "h6": 0, "h24": 0},
        volume={"m5": 0, "h1": 0, "h6": 0, "h24": 0},
        txns={"m5": {"buys": 0, "sells": 0}, "h1": {"buys": 0, "sells": 0}},
    )
    base.update(kw)
    return TokenSnapshot(**base)


def test_strong_pump_scores_high():
    s = _snap(
        price_change={"m5": 4, "h1": 28, "h6": 60, "h24": 90},
        volume={"m5": 9000, "h1": 90000, "h6": 200000, "h24": 300000},
        txns={"m5": {"buys": 40, "sells": 8}, "h1": {"buys": 220, "sells": 70}},
    )
    d = detect(s, CFG)
    assert d.pump_score >= 70, d.pump_score
    assert d.dump_risk < 50
    assert d.phase in ("markup", "accumulation")


def test_distribution_flags_dump_risk():
    s = _snap(
        price_change={"m5": -3.5, "h1": -6, "h6": 70, "h24": 150},
        volume={"m5": 1000, "h1": 12000, "h6": 240000, "h24": 320000},
        txns={"m5": {"buys": 5, "sells": 30}, "h1": {"buys": 60, "sells": 180}},
    )
    d = detect(s, CFG)
    assert d.dump_risk >= 55, d.dump_risk
    assert d.phase in ("distribution", "dump")
    assert "sell_pressure" in d.flags or "rollover" in d.flags


def test_thin_liquidity_penalises_safety():
    s = _snap(liquidity_usd=4000,
              price_change={"m5": 5, "h1": 20, "h6": 30, "h24": 40},
              volume={"m5": 2000, "h1": 30000, "h6": 60000, "h24": 90000},
              txns={"m5": {"buys": 20, "sells": 5}, "h1": {"buys": 100, "sells": 40}})
    d = detect(s, CFG)
    assert d.safety < CFG.entry_min_safety
    assert "thin_liquidity" in d.flags


def test_scores_are_bounded():
    s = _snap(
        price_change={"m5": 99, "h1": 500, "h6": 900, "h24": 2000},
        volume={"m5": 50000, "h1": 500000, "h6": 900000, "h24": 1000000},
        txns={"m5": {"buys": 999, "sells": 1}, "h1": {"buys": 9999, "sells": 5}},
    )
    d = detect(s, CFG)
    assert 0 <= d.pump_score <= 100
    assert 0 <= d.dump_risk <= 100
    assert 0 <= d.safety <= 100
    assert 0 <= d.opportunity <= 100


def test_quiet_token_low_scores():
    s = _snap(
        price_change={"m5": 0.1, "h1": 0.5, "h6": 1, "h24": 2},
        volume={"m5": 200, "h1": 3000, "h6": 18000, "h24": 70000},
        txns={"m5": {"buys": 3, "sells": 3}, "h1": {"buys": 30, "sells": 28}},
    )
    d = detect(s, CFG)
    assert d.pump_score < CFG.entry_min_pump_score
