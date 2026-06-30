"""Cabal-buy auto-entry gate tests (pure decision function)."""
from app.config import Config
from app.engine.scanner import cabal_buy_decision


def _bundle(**kw):
    base = dict(address="Mint1", symbol="X", wallet_count=4, span_seconds=300,
                cabal_id="cabal-2", label="synchronized",
                wallets=[{"wallet_short": "aa..bb"}])
    base.update(kw)
    return base


def _det(**kw):
    base = dict(safety=70, dump_risk=20, phase="markup")
    base.update(kw)
    return base


def test_passes_clean_cabal_pile_in():
    ok, reason = cabal_buy_decision(_bundle(), _det(), Config())
    assert ok and "cleared" in reason


def test_rejects_when_disabled():
    cfg = Config(); cfg.cabal_buy_enabled = False
    ok, _ = cabal_buy_decision(_bundle(), _det(), cfg)
    assert not ok


def test_rejects_too_few_wallets():
    ok, reason = cabal_buy_decision(_bundle(wallet_count=2), _det(), Config())
    assert not ok and "few wallets" in reason


def test_rejects_outside_window():
    ok, reason = cabal_buy_decision(_bundle(span_seconds=5000), _det(), Config())
    assert not ok and "time window" in reason


def test_rejects_when_not_a_known_cabal_and_required():
    cfg = Config()  # cabal_buy_require_cabal defaults True
    ok, reason = cabal_buy_decision(_bundle(cabal_id=None), _det(), cfg)
    assert not ok and "known cabal" in reason


def test_allows_any_bundle_when_cabal_not_required():
    cfg = Config(); cfg.cabal_buy_require_cabal = False
    ok, _ = cabal_buy_decision(_bundle(cabal_id=None), _det(), cfg)
    assert ok


def test_rejects_low_safety():
    ok, reason = cabal_buy_decision(_bundle(), _det(safety=30), Config())
    assert not ok and "safety" in reason


def test_rejects_high_dump_risk():
    ok, reason = cabal_buy_decision(_bundle(), _det(dump_risk=80), Config())
    assert not ok and "dump risk" in reason


def test_rejects_dumping_phase():
    ok, reason = cabal_buy_decision(_bundle(), _det(phase="dump"), Config())
    assert not ok and "dumping" in reason


def test_rejects_when_coin_not_on_board():
    ok, reason = cabal_buy_decision(_bundle(), None, Config())
    assert not ok and "not analysed" in reason
