"""Launchpad tests — moonshot scoring, creator discovery, and spray paper bets.

All deterministic and network-free: the ingester is exercised by feeding it raw
PumpPortal-shaped messages, and the spray engine is driven with hand-built board
entries + snapshots so no websocket / HTTP is involved.
"""
import time

import pytest

from app.config import Config
from app.database import Database
from app.data.pumpportal import (PUMP_TOTAL_SUPPLY, PumpPortalIngester,
                                 buyer_smart_score)
from app.engine.launchpad import LaunchpadEngine, effective_liquidity, moonshot_score
from app.engine.paper_trader import PaperTrader
from app.models import TokenSnapshot, now


def _snap(**kw):
    base = dict(
        address="mint", symbol="X", name="x", chain="solana", pair_address="p",
        dex="pump", url="u", price_usd=0.0001, liquidity_usd=12000, fdv=40000,
        market_cap=40000, pair_created_at_ms=int((time.time() - 120) * 1000),
    )
    base.update(kw)
    return TokenSnapshot(**base)


def _coin(**kw):
    base = dict(mint="mint", symbol="X", name="x", creator="Crtr", created_at=now(),
                init_mcap_sol=30.0, sol_reserve=30.0, token_reserve=1.07e9,
                initial_buy=5e6, uri="ipfs://x", pool="pump", migrated=False)
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def test_runner_outscores_rug():
    cfg = Config()
    runner = _snap(price_change={"m5": 35, "h1": 120},
                   volume={"m5": 12000, "h1": 40000},
                   txns={"m5": {"buys": 40, "sells": 8}, "h1": {"buys": 120, "sells": 40}})
    r = moonshot_score(runner, _coin(), {"hit_rate": 60, "graduated": 2,
                                         "launches": 5, "tractions": 1}, cfg, 150)
    rug = _snap(liquidity_usd=0, price_change={"m5": -22, "h1": -10},
                volume={"m5": 200, "h1": 400},
                txns={"m5": {"buys": 4, "sells": 20}, "h1": {"buys": 20, "sells": 50}})
    g = moonshot_score(rug, _coin(initial_buy=3e8, sol_reserve=0.0),
                       {"hit_rate": 0, "graduated": 0, "launches": 6, "tractions": 0}, cfg, 150)
    assert r["score"] > 65 > g["score"]
    assert r["phase"] == "running" and r["live"] is True
    assert "dumping" in g["flags"] and "dev_heavy" in g["flags"] and "serial_launcher" in g["flags"]


def test_pre_market_when_no_snapshot():
    cfg = Config()
    r = moonshot_score(None, _coin(), {"hit_rate": 70, "graduated": 3,
                                       "launches": 4, "tractions": 0}, cfg, 150)
    assert r["live"] is False and "pre_market" in r["flags"] and r["phase"] == "forming"


def test_dev_heavy_flagged():
    cfg = Config()
    r = moonshot_score(_snap(), _coin(initial_buy=0.4 * PUMP_TOTAL_SUPPLY), None, cfg, 150)
    assert "dev_heavy" in r["flags"]


def test_effective_liquidity_floors_with_bonding_curve():
    # DexScreener reads $0 for a fresh coin, but the curve has ~30 SOL of liquidity
    s = _snap(liquidity_usd=0.0)
    liq = effective_liquidity(s, _coin(sol_reserve=30.0), 70.0)
    assert liq == pytest.approx(2 * 30 * 70)            # both sides of the AMM
    # a real indexed liquidity wins when larger
    assert effective_liquidity(_snap(liquidity_usd=50000), _coin(), 70.0) == 50000


# --------------------------------------------------------------------------- #
# Ingester (fed raw PumpPortal messages)
# --------------------------------------------------------------------------- #
def _ing():
    return PumpPortalIngester(Config())


def test_ingester_tracks_coins_and_creators():
    ing = _ing()
    ing._handle('{"message":"Successfully subscribed"}')   # ack ignored
    ing._handle('{"txType":"create","mint":"M1","name":"a","symbol":"A",'
                '"traderPublicKey":"DEV","marketCapSol":30,"vSolInBondingCurve":30,'
                '"vTokensInBondingCurve":1.07e9,"initialBuy":5e6,"uri":"u","pool":"pump"}')
    assert ing.total_coins_seen == 1
    assert ing.coin("M1")["creator"] == "DEV"
    rec = ing.creator_record("DEV")
    assert rec and rec["launches"] == 1 and rec["graduated"] == 0


def test_ingester_migration_credits_creator():
    ing = _ing()
    ing._on_create({"txType": "create", "mint": "M1", "traderPublicKey": "DEV",
                    "symbol": "A", "name": "a"})
    ing._on_create({"txType": "create", "mint": "M2", "traderPublicKey": "DEV",
                    "symbol": "B", "name": "b"})
    ing._on_migrate({"txType": "migrate", "mint": "M1"})
    rec = ing.creator_record("DEV")
    assert rec["graduated"] == 1 and rec["launches"] == 2
    assert ing.coin("M1")["migrated"] is True
    disc = ing.discovered_creators()
    assert any(d["wallet"] == "DEV" and d["graduated"] == 1 for d in disc)


def test_ingester_note_traction_marks_hit():
    ing = _ing()
    ing._on_create({"txType": "create", "mint": "M1", "traderPublicKey": "DEV",
                    "symbol": "A", "name": "a"})
    ing.note_traction("M1", liq_usd=cfg_val("creator_traction_liq_usd") + 1, vol_h1_usd=0)
    assert ing.creator_record("DEV")["tractions"] == 1
    assert ing.coin("M1")["traction"] is True


def test_recent_coins_age_filter():
    ing = _ing()
    ing._on_create({"txType": "create", "mint": "OLD", "traderPublicKey": "D", "symbol": "O", "name": "o"})
    ing._coins["OLD"]["created_at"] = now() - 9999  # force old
    ing._on_create({"txType": "create", "mint": "NEW", "traderPublicKey": "D", "symbol": "N", "name": "n"})
    fresh = {c["mint"] for c in ing.recent_coins(max_age_minutes=30)}
    assert "NEW" in fresh and "OLD" not in fresh


def cfg_val(name):
    return getattr(Config(), name)


# --------------------------------------------------------------------------- #
# Buyer smart-scoring + crediting (per-trade stream)
# --------------------------------------------------------------------------- #
def test_buyer_score_proven_beats_dumper_and_lucky():
    # proven: 10 coins, 7 winners incl 3 graduations, net accumulator
    proven = buyer_smart_score(buys=30, sells=8, sol_in=25, sol_out=5,
                               coins=10, wins=7, grads=3)
    # dumper: lots of selling, net negative, barely any winners
    dumper = buyer_smart_score(buys=4, sells=30, sol_in=3, sol_out=20,
                               coins=8, wins=1, grads=0)
    # lucky one-shot: 1 coin, 1 win — must be dampened below proven
    lucky = buyer_smart_score(buys=1, sells=0, sol_in=1, sol_out=0,
                              coins=1, wins=1, grads=0)
    assert proven > 70 > lucky and lucky > dumper
    assert dumper < 25


def test_buyer_credited_when_bought_coin_wins():
    ing = _ing()
    ing._on_create({"txType": "create", "mint": "M1", "traderPublicKey": "DEV",
                    "symbol": "A", "name": "a"})
    # two wallets buy M1 early (as the trade stream would report)
    for w in ("WhaleA", "WhaleB"):
        ing._on_trade({"txType": "buy", "mint": "M1", "traderPublicKey": w, "solAmount": 2})
    # M1 gains traction, then graduates
    ing.note_traction("M1", liq_usd=cfg_val("creator_traction_liq_usd") + 1, vol_h1_usd=0)
    ing._on_migrate({"txType": "migrate", "mint": "M1"})
    buyers = {b["wallet"]: b for b in ing.discovered_buyers()}
    assert buyers["WhaleA"]["wins"] == 1 and buyers["WhaleA"]["grads"] == 1
    assert buyers["WhaleA"]["hit_rate"] == 100
    # a seller who never bought isn't credited as a buyer
    ing._on_trade({"txType": "sell", "mint": "M1", "traderPublicKey": "Seller", "solAmount": 9})
    assert ing.buyer_record("Seller")["coins"] == 0


def test_buyers_empty_without_key_flag():
    # the engine only exposes buyers when the funded-key flag is on; here we just
    # confirm the ingester view is independent and computes coins correctly
    ing = _ing()
    ing._on_trade({"txType": "buy", "mint": "Z", "traderPublicKey": "W", "solAmount": 1})
    assert ing.discovered_buyers()[0]["coins"] == 1


# --------------------------------------------------------------------------- #
# Spray paper trading (engine driven directly, no network)
# --------------------------------------------------------------------------- #
class _StubDex:
    async def enrich(self, mints):
        return []

    async def price_for(self, addr):
        return 70.0


class _StubIngester:
    """Minimal ingester surface used by the spray paths."""
    def __init__(self, coins):
        self._coins = {c["mint"]: c for c in coins}

    def coin(self, mint):
        return self._coins.get(mint)


@pytest.fixture
def engine(tmp_path):
    cfg = Config()
    cfg.db_path = str(tmp_path / "t.db")
    cfg.spray_enabled = True
    cfg.spray_bet_usd = 25
    cfg.spray_min_score = 70
    cfg.spray_min_liquidity_usd = 3000
    cfg.spray_take_profit_mult = 3.0
    db = Database(cfg.db_path)
    trader = PaperTrader(cfg, db)
    signals = []
    coin = _coin(mint="HOT", symbol="HOT")
    ing = _StubIngester([coin])
    eng = LaunchpadEngine(cfg, _StubDex(), ing, trader, signals.append)
    return eng, trader, cfg, signals, coin


def _board_entry(mint="HOT", score=85.0):
    return {"mint": mint, "symbol": "HOT", "moonshot": score, "live": True,
            "flags": [], "phase": "running", "reasons": ["+200% 5m"],
            "creator": "Crtr", "creator_short": "Crtr", "creator_rec": {"hit_rate": 60},
            "age_seconds": 90, "age_minutes": 1.5}


def test_spray_opens_and_caps(engine):
    eng, trader, cfg, signals, coin = engine
    snap = _snap(address="HOT", symbol="HOT", liquidity_usd=0.0,
                 volume={"h1": 8000}, price_usd=0.0002)
    eng._open_spray([_board_entry()], {"HOT": snap})
    assert "HOT" in trader.positions
    pos = trader.positions["HOT"]
    assert PaperTrader.is_spray(pos) and pos.entry_value == pytest.approx(25, abs=0.5)
    # spray bets do NOT count against the core cap
    assert trader.core_positions() == 0
    # already sprayed -> not re-opened
    eng._open_spray([_board_entry()], {"HOT": snap})
    assert len([p for p in trader.positions.values() if PaperTrader.is_spray(p)]) == 1


def test_spray_below_score_or_volume_skipped(engine):
    eng, trader, cfg, signals, coin = engine
    weak_snap = _snap(address="HOT", liquidity_usd=0.0, volume={"h1": 100}, price_usd=0.0002)
    eng._open_spray([_board_entry(score=85)], {"HOT": weak_snap})   # no tradeable market
    assert "HOT" not in trader.positions
    good_snap = _snap(address="HOT", liquidity_usd=0.0, volume={"h1": 8000}, price_usd=0.0002)
    eng._open_spray([_board_entry(score=55)], {"HOT": good_snap})   # below spray_min_score
    assert "HOT" not in trader.positions


def test_spray_takes_profit_on_moonshot(engine):
    eng, trader, cfg, signals, coin = engine
    snap = _snap(address="HOT", symbol="HOT", liquidity_usd=0.0,
                 volume={"h1": 8000}, price_usd=0.0002)
    eng._open_spray([_board_entry()], {"HOT": snap})
    entry = trader.positions["HOT"].entry_price
    # price 4x -> above the 3x take-profit
    moon = _snap(address="HOT", symbol="HOT", liquidity_usd=0.0,
                 volume={"h1": 9000}, price_usd=entry * 4)
    eng._manage_spray({"HOT": moon})
    assert "HOT" not in trader.positions          # exited
    trades = trader.db.recent_trades(5)
    assert trades and trades[0]["pnl"] > 0
    assert trades[0]["entry_context"]["kind"] == "spray"
    assert any(s.kind == "spray" for s in signals)


def test_spray_timeout_bails_when_flat(engine):
    eng, trader, cfg, signals, coin = engine
    snap = _snap(address="HOT", symbol="HOT", liquidity_usd=0.0,
                 volume={"h1": 8000}, price_usd=0.0002)
    eng._open_spray([_board_entry()], {"HOT": snap})
    trader.positions["HOT"].opened_at = now() - cfg.spray_max_hold_minutes * 60 - 5
    eng._manage_spray({"HOT": snap})
    assert "HOT" not in trader.positions
