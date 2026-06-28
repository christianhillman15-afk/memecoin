"""Pump & dump detection.

Pure, deterministic scoring over a :class:`TokenSnapshot`. No I/O, so it is
fully unit-testable. Produces:

* ``pump_score``  0-100  — strength of an active upward pump / momentum
* ``dump_risk``   0-100  — likelihood of imminent distribution / dump
* ``safety``      0-100  — structural safety (liquidity, age, churn sanity)
* ``phase``       market-cycle label (accumulation/markup/distribution/dump/quiet)
* ``opportunity`` 0-100  — blended ranking score for the dashboard

The heuristics are intentionally explainable: every score appends human-readable
``reasons`` so a trader can see *why* a coin was flagged.
"""
from __future__ import annotations

from ..config import Config
from ..models import DetectionResult, TokenSnapshot


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _buy_ratio(snap: TokenSnapshot, window: str) -> float:
    b, s = snap.buys(window), snap.sells(window)
    t = b + s
    return (b / t) if t else 0.5


def detect(snap: TokenSnapshot, cfg: Config) -> DetectionResult:
    reasons: list[str] = []
    flags: list[str] = []

    ch_m5 = snap.price_change.get("m5", 0.0)
    ch_h1 = snap.price_change.get("h1", 0.0)
    ch_h6 = snap.price_change.get("h6", 0.0)
    ch_h24 = snap.price_change.get("h24", 0.0)

    vol_m5 = snap.volume.get("m5", 0.0)
    vol_h1 = snap.volume.get("h1", 0.0)
    vol_h6 = snap.volume.get("h6", 0.0)
    vol_h24 = snap.volume.get("h24", 0.0)

    br_m5 = _buy_ratio(snap, "m5")
    br_h1 = _buy_ratio(snap, "h1")

    # ---------------- PUMP SCORE ---------------- #
    pump = 0.0
    # 1) short-term price acceleration
    if ch_h1 > 0:
        pump += _clamp(ch_h1 * 1.4, 0, 35)
        if ch_h1 > 15:
            reasons.append(f"+{ch_h1:.0f}% in 1h")
    if ch_m5 > 0:
        pump += _clamp(ch_m5 * 2.0, 0, 18)
    # 2) volume surge: recent hourly run-rate vs the 24h average hour
    avg_hour = (vol_h24 / 24.0) if vol_h24 else 0.0
    if avg_hour > 0 and vol_h1 > 0:
        surge = vol_h1 / avg_hour
        if surge > 1.2:
            pump += _clamp((surge - 1.0) * 14, 0, 28)
        if surge > 3:
            reasons.append(f"{surge:.1f}x volume surge")
            flags.append("volume_surge")
    # 3) 5-minute run-rate hot vs the hour
    if vol_h1 > 0 and vol_m5 * 12 > vol_h1 * 1.5:
        pump += 8
        reasons.append("accelerating last 5m")
    # 4) buy-side dominance
    if br_h1 > 0.55:
        pump += _clamp((br_h1 - 0.5) * 80, 0, 16)
    if br_m5 > 0.6:
        pump += _clamp((br_m5 - 0.5) * 40, 0, 8)
        reasons.append(f"{br_m5*100:.0f}% buys (5m)")
    # 5) youth bonus — fresh coins pump hardest
    age = snap.age_minutes
    if age < 360:
        pump += 8
    if age < 90:
        pump += 6
    pump = _clamp(pump)

    # ---------------- DUMP RISK ----------------- #
    dump = 0.0
    # already ran a lot recently -> distribution risk
    if ch_h6 > 60:
        dump += _clamp((ch_h6 - 60) * 0.25, 0, 22)
    if ch_h24 > 120:
        dump += 8
    # short-term rollover after the run
    if ch_h6 > 25 and ch_m5 < 0:
        dump += 18
        reasons.append("rolling over (5m red)")
        flags.append("rollover")
    if ch_h1 < 0 and ch_h6 > 25:
        dump += 14
    # sell-side dominance building
    if br_h1 < 0.45:
        dump += _clamp((0.5 - br_h1) * 90, 0, 22)
        flags.append("sell_pressure")
    if br_m5 < 0.4:
        dump += _clamp((0.5 - br_m5) * 50, 0, 12)
        reasons.append(f"{(1-br_m5)*100:.0f}% sells (5m)")
    # volume fading while price elevated = quiet distribution
    if vol_h1 > 0 and avg_hour > 0 and vol_h1 < avg_hour * 0.6 and ch_h6 > 20:
        dump += 10
        reasons.append("volume fading at highs")
    dump = _clamp(dump)

    # ---------------- SAFETY -------------------- #
    safety = 70.0
    if snap.liquidity_usd < cfg.min_liquidity_usd:
        safety -= 35
        flags.append("thin_liquidity")
    elif snap.liquidity_usd < cfg.min_liquidity_usd * 2:
        safety -= 12
    if snap.liquidity_usd > cfg.min_liquidity_usd * 6:
        safety += 10
    # churn: huge volume vs liquidity = wash / hot-potato
    if snap.liquidity_usd > 0:
        churn = vol_h1 / snap.liquidity_usd
        if churn > 4:
            safety -= 18
            flags.append("high_churn")
        elif churn > 1.5:
            safety -= 6
    # extreme FDV vs liquidity = supply overhang
    if snap.fdv and snap.liquidity_usd and snap.liquidity_usd / snap.fdv < 0.015:
        safety -= 15
        flags.append("supply_overhang")
    # too-young pools are dangerous
    if age < cfg.min_pair_age_minutes:
        safety -= 25
        flags.append("very_new")
    safety = _clamp(safety)

    # ---------------- MOMENTUM ------------------ #
    momentum = round(ch_m5 * 0.5 + ch_h1 * 0.35 + ch_h6 * 0.1, 2)

    # ---------------- PHASE --------------------- #
    phase = _phase(ch_m5, ch_h1, ch_h6, br_h1, pump, dump)

    # ---------------- OPPORTUNITY --------------- #
    # reward pump strength + safety, penalise dump risk
    opportunity = _clamp(pump * 0.6 + safety * 0.25 - dump * 0.35 + 20)
    if "thin_liquidity" in flags or "high_churn" in flags:
        opportunity *= 0.7

    if not reasons:
        reasons.append("no strong signal")

    return DetectionResult(
        address=snap.address, symbol=snap.symbol,
        pump_score=round(pump, 1), dump_risk=round(dump, 1),
        momentum=momentum, safety=round(safety, 1), phase=phase,
        opportunity=round(opportunity, 1), reasons=reasons, flags=flags,
    )


def _phase(ch_m5: float, ch_h1: float, ch_h6: float,
           br_h1: float, pump: float, dump: float) -> str:
    if ch_h6 > 25 and (ch_m5 < -1 or ch_h1 < -3) and dump > 50:
        return "dump"
    if ch_h6 > 30 and ch_m5 <= 0:
        return "distribution"
    if pump > 55 and ch_h1 > 0:
        return "markup"
    if -5 < ch_h1 < 8 and br_h1 >= 0.5 and ch_h6 > -10:
        return "accumulation"
    return "quiet"
