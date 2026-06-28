"""Configuration loading: YAML file + environment overrides for secrets."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

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

    # engine
    auto_trade: bool = True
    log_level: str = "INFO"

    # secrets / runtime (from env, not persisted)
    helius_api_key: str = field(default="", repr=False)
    birdeye_api_key: str = field(default="", repr=False)
    host: str = "127.0.0.1"
    port: int = 8000
    db_path: str = str(ROOT / "memeradar.db")

    @property
    def has_wallet_provider(self) -> bool:
        return bool(self.helius_api_key)

    def public_dict(self) -> dict[str, Any]:
        """Config safe to expose to the dashboard (no secrets)."""
        out: dict[str, Any] = {}
        secret = {"helius_api_key", "birdeye_api_key", "db_path"}
        for f in fields(self):
            if f.name in secret:
                continue
            out[f.name] = getattr(self, f.name)
        out["wallet_provider"] = "helius" if self.has_wallet_provider else "simulated"
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
    cfg.host = os.environ.get("MEMEBOT_HOST", cfg.host)
    cfg.port = int(os.environ.get("MEMEBOT_PORT", cfg.port))
    if os.environ.get("MEMEBOT_DB"):
        cfg.db_path = os.environ["MEMEBOT_DB"]
    return cfg
