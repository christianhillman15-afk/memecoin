"""Paper trader tests — fills, fees, P&L, persistence."""
import time

import pytest

from app.config import Config
from app.database import Database
from app.engine.paper_trader import PaperTrader
from app.models import TokenSnapshot


def _snap(price=0.001, **kw):
    base = dict(
        address="Mint1", symbol="DOGE", name="Doge", chain="solana",
        pair_address="P", dex="raydium", url="u", price_usd=price,
        liquidity_usd=100000, fdv=1e6, market_cap=1e6,
        pair_created_at_ms=int(time.time() * 1000) - 3600_000,
    )
    base.update(kw)
    return TokenSnapshot(**base)


@pytest.fixture
def trader(tmp_path):
    cfg = Config()
    cfg.db_path = str(tmp_path / "t.db")
    db = Database(cfg.db_path)
    return PaperTrader(cfg, db), cfg, db


def test_starts_with_bankroll(trader):
    t, cfg, _ = trader
    assert t.cash == cfg.starting_balance_usd == 10000
    assert t.equity() == 10000


def test_open_deducts_cash_and_holds_qty(trader):
    t, cfg, _ = trader
    pos = t.open_position(_snap(price=0.001), "test entry")
    assert pos is not None
    assert t.cash < 10000
    assert pos.qty > 0
    # entry fill is above mid (slippage), so qty*entry_price ~ size*(1-fee)
    assert pos.entry_price > 0.001
    assert t.has_position("Mint1")


def test_profitable_exit_increases_equity(trader):
    t, cfg, db = trader
    t.open_position(_snap(price=0.001), "entry")
    # price doubles
    t.mark("Mint1", 0.002)
    trade = t.close_position("Mint1", 0.002, "take-profit")
    assert trade is not None
    assert trade.pnl > 0
    assert not t.has_position("Mint1")
    st = t.portfolio_state()
    assert st.equity > 10000
    assert st.wins == 1 and st.losses == 0


def test_loss_exit_recorded(trader):
    t, cfg, db = trader
    t.open_position(_snap(price=0.001), "entry")
    t.mark("Mint1", 0.0005)
    trade = t.close_position("Mint1", 0.0005, "stop-loss")
    assert trade.pnl < 0
    st = t.portfolio_state()
    assert st.losses == 1


def test_max_open_positions_enforced(trader):
    t, cfg, _ = trader
    for i in range(cfg.max_open_positions + 3):
        t.open_position(_snap(price=0.001, address=f"Mint{i}", symbol=f"T{i}"), "e")
    assert len(t.positions) <= cfg.max_open_positions


def test_persistence_round_trip(trader):
    t, cfg, db = trader
    t.open_position(_snap(price=0.001), "entry")
    cash_after = t.cash
    # rebuild from the same DB
    t2 = PaperTrader(cfg, db)
    assert t2.has_position("Mint1")
    assert abs(t2.cash - cash_after) < 1e-6


def test_reset_restores_bankroll(trader):
    t, cfg, db = trader
    t.open_position(_snap(price=0.001), "entry")
    t.mark("Mint1", 0.002)
    t.close_position("Mint1", 0.002, "tp")
    t.reset()
    assert t.cash == 10000
    assert t.portfolio_state().total_trades == 0
