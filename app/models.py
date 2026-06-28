"""Domain models shared across the engine, storage and API layers."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


def now() -> float:
    return time.time()


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #
@dataclass
class TokenSnapshot:
    """A point-in-time view of one token's most liquid pair."""

    address: str
    symbol: str
    name: str
    chain: str
    pair_address: str
    dex: str
    url: str
    price_usd: float
    liquidity_usd: float
    fdv: float
    market_cap: float
    pair_created_at_ms: int
    # multi-window change %, e.g. {"m5": 3.2, "h1": -1.1, "h6": 40.0, "h24": 120.0}
    price_change: dict[str, float] = field(default_factory=dict)
    volume: dict[str, float] = field(default_factory=dict)
    txns: dict[str, dict[str, int]] = field(default_factory=dict)
    fetched_at: float = field(default_factory=now)
    # on-chain rug checks (None = not checked / unknown)
    mint_renounced: bool | None = None      # mint authority renounced?
    freeze_renounced: bool | None = None     # freeze authority renounced?

    @property
    def age_minutes(self) -> float:
        if not self.pair_created_at_ms:
            return 0.0
        return max(0.0, (now() * 1000 - self.pair_created_at_ms) / 60000.0)

    def buys(self, window: str) -> int:
        return int(self.txns.get(window, {}).get("buys", 0))

    def sells(self, window: str) -> int:
        return int(self.txns.get(window, {}).get("sells", 0))

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["age_minutes"] = round(self.age_minutes, 1)
        return d


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
@dataclass
class DetectionResult:
    address: str
    symbol: str
    pump_score: float       # 0-100, likelihood of an active pump / momentum
    dump_risk: float        # 0-100, likelihood of an imminent dump / distribution
    momentum: float         # signed, recent directional strength
    safety: float           # 0-100, structural safety (liquidity/age/etc.)
    phase: str              # accumulation | markup | distribution | dump | quiet
    opportunity: float      # 0-100, blended ranking score
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Wallet intelligence
# --------------------------------------------------------------------------- #
@dataclass
class WalletEvent:
    wallet: str
    token: str
    symbol: str
    side: str               # buy | sell
    usd: float
    kind: str               # whale | insider | smart_money | retail
    source: str             # helius | simulated
    win_rate: float = 0.0   # historical hit-rate of this wallet (0-1)
    ts: float = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["wallet_short"] = self.wallet[:4] + ".." + self.wallet[-4:]
        return d


@dataclass
class TokenIntel:
    """Aggregated wallet-level intelligence for a single token."""

    address: str
    symbol: str
    smart_inflow_usd: float = 0.0     # net smart/whale buying (positive = inflow)
    smart_inflow_score: float = 0.0   # -100..100 normalised
    whale_count: int = 0
    insider_count: int = 0
    smart_money_count: int = 0
    whale_concentration: float = 0.0  # 0-100, top-holder dominance
    confirmed_multi_sell: bool = False
    multi_sell_wallets: int = 0
    multi_sell_usd: float = 0.0
    source: str = "simulated"
    recent_events: list[WalletEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["recent_events"] = [e.to_dict() for e in self.recent_events]
        return d


# --------------------------------------------------------------------------- #
# Signals / alerts
# --------------------------------------------------------------------------- #
@dataclass
class Signal:
    kind: str               # pump | dump | whale_buy | multi_sell | entry | exit
    severity: str           # info | warning | critical | success
    token: str
    symbol: str
    message: str
    ts: float = field(default_factory=now)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Trading
# --------------------------------------------------------------------------- #
@dataclass
class Position:
    address: str
    symbol: str
    name: str
    url: str
    qty: float              # token units held
    entry_price: float
    entry_value: float      # USD deployed (after fees/slippage)
    opened_at: float
    peak_price: float
    last_price: float
    entry_reason: str = ""
    trailing_armed: bool = False
    high_water_pnl_pct: float = 0.0
    entry_context: dict = field(default_factory=dict)  # why we bought (scores/intel)

    @property
    def market_value(self) -> float:
        return self.qty * self.last_price

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.entry_value

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.entry_value <= 0:
            return 0.0
        return (self.market_value - self.entry_value) / self.entry_value * 100.0

    @property
    def hold_seconds(self) -> float:
        return now() - self.opened_at

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(
            market_value=round(self.market_value, 2),
            unrealized_pnl=round(self.unrealized_pnl, 2),
            unrealized_pnl_pct=round(self.unrealized_pnl_pct, 2),
            hold_seconds=round(self.hold_seconds, 0),
        )
        return d


@dataclass
class Trade:
    address: str
    symbol: str
    name: str
    qty: float
    entry_price: float
    exit_price: float
    entry_value: float
    exit_value: float
    pnl: float
    pnl_pct: float
    opened_at: float
    closed_at: float
    entry_reason: str
    exit_reason: str
    entry_context: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PortfolioState:
    cash: float
    equity: float
    starting_balance: float
    realized_pnl: float
    unrealized_pnl: float
    open_positions: int
    total_trades: int
    wins: int
    losses: int

    @property
    def total_pnl(self) -> float:
        return self.equity - self.starting_balance

    @property
    def roi_pct(self) -> float:
        if self.starting_balance <= 0:
            return 0.0
        return (self.equity - self.starting_balance) / self.starting_balance * 100.0

    @property
    def win_rate(self) -> float:
        closed = self.wins + self.losses
        return (self.wins / closed * 100.0) if closed else 0.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(
            total_pnl=round(self.total_pnl, 2),
            roi_pct=round(self.roi_pct, 2),
            win_rate=round(self.win_rate, 1),
        )
        return d
