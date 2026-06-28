"""Entry / exit strategy — the decision layer.

Entries require a confluence of: a strong pump signal, acceptable dump risk,
structural safety, and (optionally) net smart-money inflow.

Exits implement the user's core ask — *"sell before the dump"* — via a layered
ladder, evaluated worst-case-first:

1. **Stop-loss**        hard floor, capital preservation.
2. **Coordinated sell** confirmed multi-wallet distribution → exit immediately.
3. **Dump-risk spike**  detector says distribution is starting → exit.
4. **Trailing stop**    once in profit, give back at most ``trailing_stop_pct``
                        from the peak — locks in a *beautiful profit* and bails
                        ahead of the crash.
5. **Take-profit**      absolute target hit.
6. **Momentum reversal**clear roll-over while up → take the money.

Each exit returns a human-readable reason that surfaces on the dashboard.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import Config
from ..models import DetectionResult, Position, TokenIntel, TokenSnapshot


@dataclass
class EntryDecision:
    enter: bool
    reason: str = ""
    confidence: float = 0.0


@dataclass
class ExitDecision:
    exit: bool
    reason: str = ""
    urgent: bool = False


def evaluate_entry(snap: TokenSnapshot, det: DetectionResult,
                   intel: TokenIntel, cfg: Config) -> EntryDecision:
    # hard gates -----------------------------------------------------------
    if snap.liquidity_usd < cfg.min_liquidity_usd:
        return EntryDecision(False, "liquidity too thin")
    if snap.liquidity_usd > cfg.max_liquidity_usd:
        return EntryDecision(False, "not a small-cap")
    if snap.age_minutes < cfg.min_pair_age_minutes:
        return EntryDecision(False, "pair too new")
    if snap.age_minutes > cfg.max_pair_age_minutes:
        return EntryDecision(False, "pair too old")
    if snap.volume.get("h1", 0.0) < cfg.min_volume_h1_usd:
        return EntryDecision(False, "insufficient recent volume")

    # on-chain rug gate: never buy a token whose dev can still mint/freeze
    if cfg.block_unrenounced_authority:
        if snap.mint_renounced is False:
            return EntryDecision(False, "mint authority not renounced (rug risk)")
        if snap.freeze_renounced is False:
            return EntryDecision(False, "freeze authority not renounced (honeypot risk)")

    # signal gates ---------------------------------------------------------
    if det.pump_score < cfg.entry_min_pump_score:
        return EntryDecision(False, f"pump score {det.pump_score:.0f} < {cfg.entry_min_pump_score:.0f}")
    if det.dump_risk > cfg.entry_max_dump_risk:
        return EntryDecision(False, f"dump risk {det.dump_risk:.0f} too high")
    if det.safety < cfg.entry_min_safety:
        return EntryDecision(False, f"safety {det.safety:.0f} too low")
    if intel.confirmed_multi_sell:
        return EntryDecision(False, "smart wallets are distributing")
    if cfg.entry_require_smart_inflow and intel.smart_inflow_score < 0:
        return EntryDecision(False, "smart money is net selling")

    # confidence: how far above the bar are we?
    conf = (
        0.45 * min(1.0, det.pump_score / 100)
        + 0.20 * min(1.0, det.safety / 100)
        + 0.20 * max(0.0, (cfg.entry_max_dump_risk - det.dump_risk) / max(1, cfg.entry_max_dump_risk))
        + 0.15 * max(0.0, intel.smart_inflow_score / 100)
    )
    reason = (f"pump {det.pump_score:.0f}, safety {det.safety:.0f}, "
              f"smart-inflow {intel.smart_inflow_score:+.0f}")
    return EntryDecision(True, reason, round(conf * 100, 1))


def evaluate_exit(pos: Position, snap: TokenSnapshot | None,
                  det: DetectionResult | None, intel: TokenIntel | None,
                  cfg: Config) -> ExitDecision:
    pnl_pct = pos.unrealized_pnl_pct / 100.0  # fractional

    # avoid instant churn right after entry (except true emergencies)
    fresh = pos.hold_seconds < cfg.min_hold_seconds

    # 1) hard stop-loss --------------------------------------------------- #
    if pnl_pct <= -cfg.stop_loss_pct:
        return ExitDecision(True, f"stop-loss hit ({pnl_pct*100:.1f}%)", urgent=True)

    # 2) confirmed coordinated distribution ------------------------------- #
    if intel and intel.confirmed_multi_sell:
        return ExitDecision(
            True,
            f"{intel.multi_sell_wallets} smart wallets dumping "
            f"(${intel.multi_sell_usd:,.0f})",
            urgent=True,
        )

    # 3) dump-risk spike -------------------------------------------------- #
    if det and det.dump_risk >= cfg.exit_on_dump_risk:
        return ExitDecision(True, f"dump risk spiked to {det.dump_risk:.0f}", urgent=True)

    if fresh:
        return ExitDecision(False)

    # 4) trailing stop (locks in profit, bails before the crash) ---------- #
    if pos.trailing_armed:
        drawdown = (pos.peak_price - pos.last_price) / pos.peak_price if pos.peak_price else 0.0
        if drawdown >= cfg.trailing_stop_pct:
            return ExitDecision(True, f"trailing stop ({drawdown*100:.1f}% off peak, "
                                      f"locked {pnl_pct*100:+.1f}%)")

    # 5) absolute take-profit -------------------------------------------- #
    if pnl_pct >= cfg.take_profit_pct:
        return ExitDecision(True, f"take-profit ({pnl_pct*100:+.1f}%)")

    # 6) momentum reversal while up -------------------------------------- #
    if cfg.exit_on_momentum_reversal and det and pnl_pct > 0.06:
        if det.phase in ("distribution", "dump") and det.momentum < 0:
            return ExitDecision(True, f"momentum reversal, banking {pnl_pct*100:+.1f}%")

    return ExitDecision(False)


def update_trailing(pos: Position, cfg: Config) -> None:
    """Update peak price + arm the trailing stop once sufficiently in profit."""
    if pos.last_price > pos.peak_price:
        pos.peak_price = pos.last_price
    pnl_pct = pos.unrealized_pnl_pct / 100.0
    pos.high_water_pnl_pct = max(pos.high_water_pnl_pct, pos.unrealized_pnl_pct)
    if not pos.trailing_armed and pnl_pct >= cfg.arm_trailing_after_pct:
        pos.trailing_armed = True
