"""Configuration loading: YAML file + environment overrides for secrets."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("memeradar.config")
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"


@dataclass
class Config:
    # universe / data
    chain: str = "solana"
    scan_interval_seconds: int = 25
    universe_size: int = 45
    min_liquidity_usd: float = 20000
    max_liquidity_usd: float = 8_000_000
    min_pair_age_minutes: int = 15
    max_pair_age_minutes: int = 20160
    min_volume_h1_usd: float = 8000

    # paper trading
    starting_balance_usd: float = 10000
    max_open_positions: int = 5
    position_size_pct: float = 0.12
    max_position_usd: float = 2500
    fee_pct: float = 0.005
    slippage_pct: float = 0.012

    # entry rules
    entry_min_pump_score: float = 62
    entry_max_dump_risk: float = 45
    entry_min_safety: float = 55
    entry_require_smart_inflow: bool = True

    # exit rules
    take_profit_pct: float = 0.45
    stop_loss_pct: float = 0.18
    trailing_stop_pct: float = 0.14
    arm_trailing_after_pct: float = 0.12
    exit_on_dump_risk: float = 72
    exit_on_momentum_reversal: bool = True
    min_hold_seconds: int = 45

    # wallet intelligence
    whale_min_usd: float = 50000
    insider_early_buy_rank: int = 25
    smart_money_min_winrate: float = 0.55
    multi_sell_window_seconds: int = 240
    multi_sell_min_wallets: int = 3
    multi_sell_min_usd: float = 15000

    # fresh loadouts (young coins a strong team is accumulating)
    loadout_max_age_minutes: int = 1440   # "new" = launched within ~24h
    loadout_min_score: float = 45
    loadout_ttl_seconds: int = 240

    # launchpad — catch pump.fun coins in their first minutes (PumpPortal WS +
    # DexScreener enrichment). Free out of the box (new-coin + migration streams);
    # a funded PUMPPORTAL_API_KEY additionally unlocks the per-trade buyer stream.
    launchpad_enabled: bool = True
    launchpad_max_age_minutes: int = 30      # how old a "fresh" coin can be
    launchpad_universe: int = 60             # max fresh coins enriched per cycle
    launchpad_keep_minutes: int = 90         # retain coins in memory this long
    moonshot_min_score: float = 55           # 0-100; surface threshold on the board
    creator_traction_liq_usd: float = 18000  # liquidity that counts as a creator "hit"
    creator_traction_vol_usd: float = 25000  # ...or this much 1h volume

    # trade-stream spend guard (only relevant with a funded PUMPPORTAL_API_KEY).
    # The per-trade stream is metered (~0.01 SOL / 10k events), so instead of
    # subscribing to every new coin we watch only the strongest few at a time.
    launchpad_trade_watch_max: int = 15      # hard cap on coins subscribed for trades
    launchpad_trade_min_score: float = 62    # only watch coins scoring >= this
    pumpportal_cost_per_10k_sol: float = 0.01  # documented metering rate (for the estimate)

    # spray mode — tiny auto paper-bets across early candidates (opt-in; it churns
    # many small positions: "each either explodes or goes to zero")
    spray_enabled: bool = False
    spray_bet_usd: float = 25                 # size of each tiny bet
    spray_max_positions: int = 20             # max concurrent spray bets
    spray_max_total_usd: float = 600          # total capital at risk in spray
    spray_min_score: float = 70               # only spray the strongest candidates
    spray_min_liquidity_usd: float = 3000     # need a real market (liq or 1h vol) to mark/exit
    spray_take_profit_mult: float = 3.0       # bank at +200% (3x)
    spray_stop_loss_pct: float = 0.55         # cut at -55%
    spray_max_hold_minutes: int = 90          # bail if it stalls

    # on-chain rug safety (free via the Solana RPC)
    safety_check_authority: bool = True       # fetch mint/freeze authority status
    block_unrenounced_authority: bool = True  # refuse to buy if mint/freeze is live

    # telegram bot — YAML-tunable alert knobs (secrets/ids come from env below)
    telegram_alerts: bool = True              # master push-alert switch
    telegram_alert_min_severity: str = "success"   # info|success|warning|critical
    telegram_alert_kinds: list = field(
        default_factory=lambda: ["dump", "multi_sell", "whale_buy", "exit", "bundle"])
    telegram_alert_cooldown_seconds: int = 900     # per-(kind,token) dedupe window
    telegram_alert_batch_seconds: int = 8          # coalesce alerts in this window
    telegram_alert_max_per_minute: int = 12        # outbound alert rate cap
    telegram_poll_timeout_seconds: int = 25        # getUpdates long-poll timeout

    # engine
    auto_trade: bool = True
    log_level: str = "INFO"

    # secrets / runtime (from env, not persisted)
    helius_api_key: str = field(default="", repr=False)
    birdeye_api_key: str = field(default="", repr=False)
    pumpportal_api_key: str = field(default="", repr=False)  # optional: unlocks trade stream
    host: str = "127.0.0.1"
    port: int = 8000
    db_path: str = str(ROOT / "memeradar.db")
    # dashboard login (HTTP basic auth) — enabled only when a password is set
    dashboard_user: str = "admin"
    dashboard_password: str = field(default="", repr=False)
    # telegram bot — env-only secrets / allowlists (empty => bot disabled)
    telegram_bot_token: str = field(default="", repr=False)
    telegram_chat_ids: frozenset = field(default_factory=frozenset)
    telegram_admin_chat_ids: frozenset = field(default_factory=frozenset)
    telegram_reset_token: str = field(default="", repr=False)

    @property
    def has_wallet_provider(self) -> bool:
        return bool(self.helius_api_key)

    @property
    def has_pumpportal_trades(self) -> bool:
        """A funded PumpPortal key unlocks the per-trade buyer stream."""
        return bool(self.pumpportal_api_key)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.dashboard_password)

    @property
    def telegram_enabled(self) -> bool:
        # only when a token AND at least one allowed read chat id are configured
        return bool(self.telegram_bot_token and self.telegram_chat_ids)

    def public_dict(self) -> dict[str, Any]:
        """Config safe to expose to the dashboard (no secrets)."""
        out: dict[str, Any] = {}
        secret = {"helius_api_key", "birdeye_api_key", "pumpportal_api_key", "db_path",
                  "dashboard_password", "dashboard_user",
                  # never leak who can read/control the desk, or the tokens
                  "telegram_bot_token", "telegram_reset_token",
                  "telegram_chat_ids", "telegram_admin_chat_ids"}
        for f in fields(self):
            if f.name in secret:
                continue
            out[f.name] = getattr(self, f.name)
        out["wallet_provider"] = "helius" if self.has_wallet_provider else "simulated"
        out["auth_enabled"] = self.auth_enabled
        out["telegram_enabled"] = self.telegram_enabled
        out["pumpportal_trades"] = self.has_pumpportal_trades
        return out


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    data: dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}

    cfg = Config()
    valid = {f.name for f in fields(Config)}
    for key, value in data.items():
        if key in valid:
            setattr(cfg, key, value)

    # environment overrides (secrets + host/port)
    cfg.helius_api_key = os.environ.get("HELIUS_API_KEY", cfg.helius_api_key)
    cfg.birdeye_api_key = os.environ.get("BIRDEYE_API_KEY", cfg.birdeye_api_key)
    cfg.pumpportal_api_key = os.environ.get("PUMPPORTAL_API_KEY", cfg.pumpportal_api_key)
    cfg.host = os.environ.get("MEMEBOT_HOST", cfg.host)
    # honour MEMEBOT_PORT, falling back to a generic PORT (PaaS convention)
    cfg.port = int(os.environ.get("MEMEBOT_PORT", os.environ.get("PORT", cfg.port)))
    if os.environ.get("MEMEBOT_DB"):
        cfg.db_path = os.environ["MEMEBOT_DB"]
    cfg.dashboard_user = os.environ.get("DASHBOARD_USER", cfg.dashboard_user)
    cfg.dashboard_password = os.environ.get("DASHBOARD_PASSWORD", cfg.dashboard_password)

    # telegram (env-only secrets + allowlists)
    cfg.telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", cfg.telegram_bot_token)
    cfg.telegram_reset_token = os.environ.get("TELEGRAM_RESET_TOKEN", cfg.telegram_reset_token)
    cfg.telegram_chat_ids = _parse_ids(os.environ.get("TELEGRAM_CHAT_IDS", ""))
    admin = _parse_ids(os.environ.get("TELEGRAM_ADMIN_CHAT_IDS", ""))
    cfg.telegram_admin_chat_ids = _resolve_admins(admin, cfg.telegram_chat_ids)
    return cfg


def _resolve_admins(admin: frozenset, readers: frozenset) -> frozenset:
    """Decide who may run destructive control commands.

    Admins must also be readers. We do NOT fail open: leaving the admin list
    empty must not silently grant every reader destructive control.
      * explicit admins  -> intersect with readers
      * unset + 1 reader -> auto-promote that sole reader (the common case)
      * unset + N readers -> no admins until explicitly set (controls locked)
    """
    if admin:
        return admin & readers
    if len(readers) == 1:
        log.warning("TELEGRAM_ADMIN_CHAT_IDS unset; auto-promoting the sole "
                    "reader to admin. Set it explicitly to be safe.")
        return readers
    if readers:
        log.warning("TELEGRAM_ADMIN_CHAT_IDS unset with %d readers; control "
                    "commands are DISABLED until you set it.", len(readers))
    return frozenset()


def _parse_ids(raw: str) -> frozenset:
    """Parse a comma-separated list of numeric chat ids into a frozenset[int].

    Tolerates whitespace and empty entries; silently drops non-numeric tokens
    (a str-vs-int mismatch would otherwise deny everyone)."""
    out: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return frozenset(out)
