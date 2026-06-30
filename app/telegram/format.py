"""Shared formatting helpers for Telegram HTML messages."""
from __future__ import annotations

import time


def h(s: object) -> str:
    """Escape text for Telegram parse_mode=HTML.

    Telegram supports only the named entities &amp; &lt; &gt; &quot;. Order
    matters: escape & FIRST so we don't double-escape the entities we emit.
    """
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def usd(n: float, d: int = 2) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "$0"
    sign = "-" if n < 0 else ""
    a = abs(n)
    if a >= 1_000_000_000:
        return f"{sign}${a/1e9:.2f}B"
    if a >= 1_000_000:
        return f"{sign}${a/1e6:.2f}M"
    if a >= 1_000:
        return f"{sign}${a/1e3:.1f}K"
    return f"{sign}${a:.{d}f}"


def pct(n: float) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0%"
    return f"{'+' if n >= 0 else ''}{n:.1f}%"


def price(n: float) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "$0"
    if n == 0:
        return "$0"
    if n < 1e-5:
        return f"${n:.2e}"
    if n < 1:
        return f"${n:.6g}"
    return f"${n:,.4f}"


def rel_time(ts: float) -> str:
    try:
        s = max(0.0, time.time() - float(ts))
    except (TypeError, ValueError):
        return "?"
    if s < 60:
        return f"{int(s)}s ago"
    if s < 3600:
        return f"{int(s/60)}m ago"
    if s < 86400:
        return f"{int(s/3600)}h ago"
    return f"{int(s/86400)}d ago"


def hold(seconds: float) -> str:
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return "?"
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{int(s/60)}m"
    return f"{s/3600:.1f}h"
