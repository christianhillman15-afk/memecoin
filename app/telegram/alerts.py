"""Signal → push-alert policy, decoupled from transport.

`should_alert` is a cheap, synchronous, in-memory filter safe to call on the
scanner's hot path (it never does I/O). Formatting and rate-limiting live here
too so the dispatcher stays thin.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..config import Config
from ..models import Signal
from .format import h, usd

SEVERITY_RANK = {"info": 0, "success": 1, "warning": 2, "critical": 3}

KIND_EMOJI = {"pump": "🚀", "dump": "🔻", "multi_sell": "🚨",
              "whale_buy": "🐋", "entry": "🟢", "exit": "🔴", "bundle": "📦"}


def dedupe_key(sig: Signal) -> tuple[str, str]:
    # keyed on (kind, token) — a coin going dump then multi_sell are distinct
    return (sig.kind, sig.token)


def should_alert(sig: Signal, cfg: Config, mutes: set[str]) -> bool:
    if not cfg.telegram_alerts:
        return False
    if sig.kind not in cfg.telegram_alert_kinds:
        return False
    floor = SEVERITY_RANK.get(cfg.telegram_alert_min_severity, 2)
    if SEVERITY_RANK.get(sig.severity, 0) < floor:
        return False
    if sig.symbol and sig.symbol.upper() in mutes:
        return False
    return True


def format_alert(sig: Signal) -> str:
    emoji = KIND_EMOJI.get(sig.kind, "•")
    line = f"{emoji} <b>{h(sig.symbol or '?')}</b> — {h(sig.message)}"
    url = (sig.meta or {}).get("url")
    if url:
        line += f'\n<a href="{h(url)}">chart ↗</a>'
    return line


def format_batch(sigs: list[Signal], suppressed: int = 0) -> str:
    if len(sigs) == 1 and not suppressed:
        return format_alert(sigs[0])
    head = f"<b>{len(sigs)} alerts</b>"
    body = "\n".join(format_alert(s) for s in sigs)
    out = f"{head}\n{body}"
    if suppressed:
        out += f"\n<i>+{suppressed} more suppressed (rate limit)</i>"
    return out


@dataclass
class AlertRateState:
    """Per-(kind,token) cooldown dedupe + a global token bucket.

    The bucket starts FULL (capacity = max_per_minute) so the first alerts go
    out immediately; sustained rate is then capped at max_per_minute/minute.
    """

    cooldown_seconds: int
    max_per_minute: int
    _last_sent: dict[tuple[str, str], float] = field(default_factory=dict)
    _bucket: float = -1.0
    _bucket_ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        # never 0/negative — that would make take_token() never refill and the
        # dispatcher busy-wait forever
        self.max_per_minute = max(1, int(self.max_per_minute))
        if self._bucket < 0:
            self._bucket = float(self.max_per_minute)

    def passes_cooldown(self, sig: Signal, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        key = dedupe_key(sig)
        last = self._last_sent.get(key, 0.0)
        if now - last < self.cooldown_seconds:
            return False
        self._last_sent[key] = now
        return True

    def take_token(self, now: float | None = None) -> bool:
        """Refill at max_per_minute/60 per second; return True if a token is free."""
        now = time.time() if now is None else now
        elapsed = max(0.0, now - self._bucket_ts)  # ignore clock going backwards
        self._bucket = min(float(self.max_per_minute),
                           self._bucket + elapsed * self.max_per_minute / 60.0)
        self._bucket_ts = now
        if self._bucket >= 1.0:
            self._bucket -= 1.0
            return True
        return False
