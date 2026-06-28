"""Telegram bot integration (long-polling, httpx only — no extra dependencies)."""
from __future__ import annotations

import logging

from ..config import Config
from .bot import TelegramBot

log = logging.getLogger("memeradar.telegram")


def build_telegram_bot(cfg: Config, scanner, db) -> "TelegramBot | None":
    """Construct the bot only when a token + read allowlist are configured.

    Mirrors ``build_wallet_provider`` / the dashboard-auth gate: dormant by
    default so local dev and tests spawn no extra tasks or network surface.
    """
    if not cfg.telegram_enabled:
        log.info("Telegram bot: disabled (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_IDS)")
        return None
    log.info("Telegram bot: enabled (%d reader(s), %d admin(s))",
             len(cfg.telegram_chat_ids), len(cfg.telegram_admin_chat_ids))
    return TelegramBot(cfg, scanner, db)
