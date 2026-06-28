"""Telegram bot unit tests — pure logic, no network."""
import os

from app.config import Config, _parse_ids, load_config
from app.models import Signal
from app.telegram import alerts as al
from app.telegram import commands as cmd
from app.telegram.format import h, pct, usd

CFG = Config()


# ---------------- allowlist parsing ----------------
def test_parse_ids_tolerant():
    assert _parse_ids("1, 2 ,,3;4") == frozenset({1, 2, 3, 4})
    assert _parse_ids("") == frozenset()
    assert _parse_ids("abc, 5, x9") == frozenset({5})  # non-numeric dropped


def test_telegram_enabled_requires_token_and_ids():
    c = Config()
    assert not c.telegram_enabled
    c.telegram_bot_token = "t"
    assert not c.telegram_enabled          # no chat ids
    c.telegram_chat_ids = frozenset({1})
    assert c.telegram_enabled


def test_admin_does_not_fail_open(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    # multiple readers + no explicit admin => controls LOCKED (not all readers)
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "10,20")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_IDS", "")
    assert load_config().telegram_admin_chat_ids == frozenset()
    # single reader + no explicit admin => auto-promote that one
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "10")
    assert load_config().telegram_admin_chat_ids == frozenset({10})
    # explicit admins are intersected with readers (no admin without read access)
    monkeypatch.setenv("TELEGRAM_CHAT_IDS", "10,20")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_IDS", "20,999")
    assert load_config().telegram_admin_chat_ids == frozenset({20})


def test_secrets_not_in_public_dict():
    c = Config()
    c.telegram_bot_token = "secret"
    c.telegram_chat_ids = frozenset({1})
    pub = c.public_dict()
    assert "telegram_bot_token" not in pub
    assert "telegram_chat_ids" not in pub
    assert pub["telegram_enabled"] is True


# ---------------- formatting ----------------
def test_html_escape():
    assert h("<b>&\"x") == "&lt;b&gt;&amp;&quot;x"
    assert h("a & b < c") == "a &amp; b &lt; c"


def test_number_formatters():
    assert usd(1500) == "$1.5K"
    assert usd(-2_000_000) == "-$2.00M"
    assert pct(12.3) == "+12.3%"
    assert pct(-4) == "-4.0%"


# ---------------- alert policy ----------------
def _sig(kind="dump", sev="critical", sym="PEPE", token="M1"):
    return Signal(kind=kind, severity=sev, token=token, symbol=sym, message="x")


def test_should_alert_kind_and_severity():
    assert al.should_alert(_sig("dump", "critical"), CFG, set())
    assert not al.should_alert(_sig("pump", "warning"), CFG, set())   # kind off by default
    assert not al.should_alert(_sig("entry", "info"), CFG, set())      # below floor
    assert al.should_alert(_sig("whale_buy", "success"), CFG, set())   # success >= floor


def test_should_alert_master_switch_and_mute():
    c = Config()
    c.telegram_alerts = False
    assert not al.should_alert(_sig(), c, set())
    assert not al.should_alert(_sig(sym="PEPE"), CFG, {"PEPE"})        # muted


def test_cooldown_dedupe():
    rs = al.AlertRateState(cooldown_seconds=100, max_per_minute=60)
    s = _sig()
    assert rs.passes_cooldown(s, now=1000)
    assert not rs.passes_cooldown(s, now=1050)   # within window
    assert rs.passes_cooldown(s, now=1200)       # after window
    # different (kind, token) is independent
    assert rs.passes_cooldown(_sig(kind="multi_sell"), now=1050)


def test_token_bucket_rate_limit():
    rs = al.AlertRateState(cooldown_seconds=0, max_per_minute=2)  # capacity 2, 2/min
    assert rs.take_token(now=0)            # starts full: 2 -> 1
    assert rs.take_token(now=0)            # 1 -> 0
    assert not rs.take_token(now=0)        # bucket empty same instant
    assert rs.take_token(now=60)           # +60s refills the bucket


def test_format_batch_groups():
    out = al.format_batch([_sig("dump", sym="A"), _sig("exit", sym="B")])
    assert "2 alerts" in out and "A" in out and "B" in out


def test_format_alert_escapes_symbol():
    out = al.format_alert(_sig(sym="<evil>"))
    assert "<evil>" not in out and "&lt;evil&gt;" in out


# ---------------- command handlers (fake scanner/db) ----------------
class _FakeIntel:
    def top_wallets(self, n):
        return [{"wallet_short": "ab..cd", "kind": "whale", "win_rate": 60.0,
                 "net_usd": 1234.0, "events": 5, "token_count": 3}]

    def categorized(self, n):
        return {"cabals": [{"id": "cabal-x", "size": 3, "sell_usd": 9000,
                            "dump_hits": 12, "token_count": 4, "shared_tokens": ["A", "B"],
                            "members": []}],
                "pump_dumpers": [], "counts": {"pump_dumpers": 7}}


class _FakeInflu:
    def snapshot(self):
        return [{"name": "KOL", "handle": "@k", "followers": 1000, "demo": True,
                 "activity": {"symbol": "ABC", "side": "buy", "usd": 5000, "source": "simulated"}}]


class _FakeScanner:
    def __init__(self):
        self.intel = _FakeIntel()
        self.influencers = _FakeInflu()

    def snapshot(self):
        return {
            "status": {"running": True, "paused": False, "scan_count": 3,
                       "last_scan_ts": 0, "last_error": None,
                       "wallet_provider": "simulated", "chain": "solana"},
            "portfolio": {"equity": 10500, "cash": 8000, "total_pnl": 500,
                          "realized_pnl": 200, "unrealized_pnl": 300, "roi_pct": 5.0,
                          "win_rate": 60, "open_positions": 1, "total_trades": 5,
                          "wins": 3, "losses": 2, "starting_balance": 10000},
            "positions": [{"address": "M1", "symbol": "PEPE", "unrealized_pnl": 50,
                           "unrealized_pnl_pct": 5.0, "entry_value": 1000,
                           "hold_seconds": 120, "trailing_armed": True,
                           "entry_reason": "pump", "url": "http://x"}],
            "board": [{"token": {"address": "M1", "symbol": "<evil>", "name": "n",
                                 "price_usd": 0.001, "age_minutes": 60,
                                 "liquidity_usd": 50000, "market_cap": 1e6, "url": "http://x",
                                 "price_change": {"m5": 1, "h1": 2, "h6": 3},
                                 "mint_renounced": True, "freeze_renounced": True},
                       "detection": {"pump_score": 80, "dump_risk": 20, "safety": 70,
                                     "opportunity": 75, "phase": "markup", "flags": [],
                                     "reasons": ["+20% 1h"], "momentum": 5},
                       "intel": {"smart_inflow_usd": 5000, "smart_inflow_score": 40,
                                 "whale_count": 2, "insider_count": 1, "smart_money_count": 1,
                                 "whale_concentration": 30, "confirmed_multi_sell": False,
                                 "multi_sell_wallets": 0, "multi_sell_usd": 0}}],
        }


class _FakeDB:
    def recent_signals(self, n):
        return [{"ts": 0, "kind": "dump", "severity": "critical", "token": "M1",
                 "symbol": "PEPE", "message": "dump risk", "meta": {}}]

    def recent_trades(self, n):
        return [{"symbol": "PEPE", "entry_price": 1, "exit_price": 2, "entry_value": 1000,
                 "pnl": 100, "pnl_pct": 10, "exit_reason": "take-profit", "closed_at": 0}]


SC, DB = _FakeScanner(), _FakeDB()


def test_cmd_portfolio():
    out = cmd.cmd_portfolio(SC, DB, CFG, [])
    assert "Portfolio" in out and "$10.5K" in out and "+5.0%" in out


def test_cmd_positions():
    out = cmd.cmd_positions(SC, DB, CFG, [])
    assert "PEPE" in out and "trailing" in out


def test_cmd_board_escapes_hostile_symbol():
    out = cmd.cmd_board(SC, DB, CFG, ["5"])
    assert "<evil>" not in out and "&lt;evil&gt;" in out


def test_cmd_token_match_and_escape():
    out = cmd.cmd_token(SC, DB, CFG, ["evil"])
    assert "&lt;evil&gt;" in out and "renounced" in out


def test_cmd_token_no_match():
    out = cmd.cmd_token(SC, DB, CFG, ["zzzzz"])
    assert "No scanned coin" in out


def test_cmd_status_and_signals_and_trades():
    assert "running" in cmd.cmd_status(SC, DB, CFG, [])
    assert "dump risk" in cmd.cmd_signals(SC, DB, CFG, [])
    assert "take-profit" in cmd.cmd_trades(SC, DB, CFG, [])


def test_cmd_cabals_and_wallets_and_influencers():
    assert "cabal-x" in cmd.cmd_cabals(SC, DB, CFG, [])
    assert "ab..cd" in cmd.cmd_wallets(SC, DB, CFG, [])
    assert "KOL" in cmd.cmd_influencers(SC, DB, CFG, [])


def test_alias_resolution():
    assert cmd.ALIASES["pos"] == "positions"
    assert cmd.ALIASES["scanner"] == "board"
    assert cmd.ALIASES["coin"] == "token"
