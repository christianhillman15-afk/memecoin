"""Strategy tests — entry gating and the 'sell before the dump' exit ladder."""
import time

from app.config import Config
from app.engine.strategy import (EntryDecision, evaluate_entry, evaluate_exit,
                                  update_trailing)
from app.models import DetectionResult, Position, TokenIntel, TokenSnapshot, now

CFG = Config()


def _snap(**kw):
    base = dict(
        address="M", symbol="WIF", name="dogwifhat", chain="solana",
        pair_address="P", dex="raydium", url="u", price_usd=0.01,
        liquidity_usd=120000, fdv=2e6, market_cap=2e6,
        pair_created_at_ms=int(time.time() * 1000) - 3 * 3600_000,
        price_change={"m5": 3, "h1": 20, "h6": 35, "h24": 60},
        volume={"m5": 5000, "h1": 60000, "h6": 150000, "h24": 240000},
        txns={"m5": {"buys": 30, "sells": 8}, "h1": {"buys": 180, "sells": 70}},
    )
    base.update(kw)
    return TokenSnapshot(**base)


def _det(pump=80, dump=20, safety=75, phase="markup", momentum=10):
    return DetectionResult("M", "WIF", pump, dump, momentum, safety, phase, 80, ["ok"], [])


def _intel(inflow=20000, score=40, multi=False, wallets=0, usd=0):
    return TokenIntel("M", "WIF", smart_inflow_usd=inflow, smart_inflow_score=score,
                      confirmed_multi_sell=multi, multi_sell_wallets=wallets,
                      multi_sell_usd=usd)


def _pos(entry=0.01, last=0.01, opened_ago=300, peak=None, armed=False, value=1000.0):
    # qty must be consistent with the USD deployed at the entry price
    qty = value / entry
    return Position(address="M", symbol="WIF", name="n", url="u", qty=qty,
                    entry_price=entry, entry_value=value, opened_at=now() - opened_ago,
                    peak_price=peak or last, last_price=last,
                    entry_reason="e", trailing_armed=armed)


# ---------------- entries ----------------
def test_entry_accepts_clean_pump():
    d = evaluate_entry(_snap(), _det(), _intel(), CFG)
    assert d.enter and d.confidence > 0


def test_entry_rejects_high_dump_risk():
    d = evaluate_entry(_snap(), _det(dump=60), _intel(), CFG)
    assert not d.enter


def test_entry_rejects_when_smart_money_selling():
    d = evaluate_entry(_snap(), _det(), _intel(score=-30), CFG)
    assert not d.enter


def test_entry_rejects_thin_liquidity():
    d = evaluate_entry(_snap(liquidity_usd=5000), _det(), _intel(), CFG)
    assert not d.enter


def test_entry_blocked_by_confirmed_multi_sell():
    d = evaluate_entry(_snap(), _det(), _intel(multi=True, wallets=4, usd=40000), CFG)
    assert not d.enter


def test_entry_blocked_when_mint_authority_live():
    d = evaluate_entry(_snap(mint_renounced=False, freeze_renounced=True), _det(), _intel(), CFG)
    assert not d.enter and "mint authority" in d.reason


def test_entry_blocked_when_freeze_authority_live():
    d = evaluate_entry(_snap(mint_renounced=True, freeze_renounced=False), _det(), _intel(), CFG)
    assert not d.enter and "freeze authority" in d.reason


def test_entry_ok_when_authorities_renounced():
    d = evaluate_entry(_snap(mint_renounced=True, freeze_renounced=True), _det(), _intel(), CFG)
    assert d.enter


# ---------------- exits ----------------
def test_stop_loss_triggers():
    pos = _pos(entry=0.01, last=0.008)  # -20%
    ex = evaluate_exit(pos, _snap(), _det(), _intel(), CFG)
    assert ex.exit and "stop-loss" in ex.reason


def test_take_profit_triggers():
    pos = _pos(entry=0.01, last=0.015)  # +50% > 45% tp
    ex = evaluate_exit(pos, _snap(), _det(), _intel(), CFG)
    assert ex.exit and "take-profit" in ex.reason


def test_confirmed_multi_sell_forces_exit():
    pos = _pos(entry=0.01, last=0.011)
    ex = evaluate_exit(pos, _snap(), _det(), _intel(multi=True, wallets=4, usd=40000), CFG)
    assert ex.exit and ex.urgent


def test_dump_risk_spike_forces_exit():
    pos = _pos(entry=0.01, last=0.011)
    ex = evaluate_exit(pos, _snap(), _det(dump=80), _intel(), CFG)
    assert ex.exit and ex.urgent


def test_trailing_stop_locks_profit():
    # armed, peaked at 0.015, pulled back to 0.012 -> 20% off peak > 14%
    pos = _pos(entry=0.01, last=0.012, peak=0.015, armed=True)
    ex = evaluate_exit(pos, _snap(), _det(), _intel(), CFG)
    assert ex.exit and "trailing" in ex.reason


def test_trailing_arms_after_threshold():
    pos = _pos(entry=0.01, last=0.0115)  # +15% > arm 12%
    update_trailing(pos, CFG)
    assert pos.trailing_armed


def test_fresh_position_not_churned():
    pos = _pos(entry=0.01, last=0.0102, opened_ago=5)  # small move, just opened
    ex = evaluate_exit(pos, _snap(), _det(), _intel(), CFG)
    assert not ex.exit


def test_fresh_position_still_stops_out_on_emergency():
    pos = _pos(entry=0.01, last=0.007, opened_ago=5)  # -30% even though fresh
    ex = evaluate_exit(pos, _snap(), _det(), _intel(), CFG)
    assert ex.exit  # stop-loss overrides min-hold
