#!/usr/bin/env python3
"""Entry point: `python run.py` starts the scanner + dashboard.

Open http://127.0.0.1:8000 once it's up.
"""
from __future__ import annotations

import os

import uvicorn

from app.config import load_config


def main() -> None:
    cfg = load_config()
    print("=" * 64)
    print("  MemeRadar — Solana memecoin pump/dump paper-trading bot")
    print("=" * 64)
    print(f"  Bankroll      : ${cfg.starting_balance_usd:,.0f} (paper)")
    print(f"  Chain         : {cfg.chain}")
    print(f"  Wallet intel  : {'Helius (live)' if cfg.has_wallet_provider else 'simulated'}")
    print(f"  Auto-trade    : {cfg.auto_trade}")
    print(f"  Dashboard     : http://{cfg.host}:{cfg.port}")
    print("=" * 64)
    uvicorn.run("app.main:app", host=cfg.host, port=cfg.port,
                log_level=cfg.log_level.lower(), reload=bool(os.environ.get("MEMEBOT_RELOAD")))


if __name__ == "__main__":
    main()
