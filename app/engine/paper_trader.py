"""Paper-trading portfolio.

Simulates fills against live prices with configurable slippage + fees. Never
touches real funds. Persists cash + open positions so a restart resumes the
exact portfolio; realised P&L / win-rate are derived from the trade ledger.
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Optional

from ..config import Config
from ..database import Database
from ..models import PortfolioState, Position, Trade, TokenSnapshot, now

log = logging.getLogger("memeradar.trader")


class PaperTrader:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.cash: float = float(db.kv_get("cash", cfg.starting_balance_usd))
        self.positions: dict[str, Position] = {}
        for addr, data in db.load_positions().items():
            try:
                self.positions[addr] = Position(**data)
            except TypeError:
                log.warning("Could not restore position %s", addr)

    # --- helpers ---------------------------------------------------------- #
    def equity(self) -> float:
        return self.cash + sum(p.market_value for p in self.positions.values())

    def can_open(self) -> bool:
        return (len(self.positions) < self.cfg.max_open_positions
                and self.cash > 50)

    def has_position(self, address: str) -> bool:
        return address in self.positions

    def _entry_size(self) -> float:
        size = self.equity() * self.cfg.position_size_pct
        size = min(size, self.cfg.max_position_usd, self.cash * 0.98)
        return max(0.0, size)

    # --- order management ------------------------------------------------- #
    def open_position(self, snap: TokenSnapshot, reason: str) -> Optional[Position]:
        if not self.can_open() or self.has_position(snap.address):
            return None
        size = self._entry_size()
        if size < 50 or snap.price_usd <= 0:
            return None
        fill = snap.price_usd * (1 + self.cfg.slippage_pct)   # buy into the spread
        qty = (size * (1 - self.cfg.fee_pct)) / fill
        pos = Position(
            address=snap.address, symbol=snap.symbol, name=snap.name, url=snap.url,
            qty=qty, entry_price=fill, entry_value=size, opened_at=now(),
            peak_price=fill, last_price=fill, entry_reason=reason,
        )
        self.cash -= size
        self.positions[snap.address] = pos
        self._persist()
        log.info("OPEN %s $%.0f @ %.8f (%s)", snap.symbol, size, fill, reason)
        return pos

    def mark(self, address: str, price: float) -> None:
        pos = self.positions.get(address)
        if pos and price > 0:
            pos.last_price = price
            if price > pos.peak_price:
                pos.peak_price = price

    def close_position(self, address: str, price: float, reason: str) -> Optional[Trade]:
        pos = self.positions.get(address)
        if not pos:
            return None
        fill = max(0.0, price) * (1 - self.cfg.slippage_pct)  # sell into the spread
        proceeds = pos.qty * fill * (1 - self.cfg.fee_pct)
        pnl = proceeds - pos.entry_value
        pnl_pct = (pnl / pos.entry_value * 100.0) if pos.entry_value else 0.0
        self.cash += proceeds
        trade = Trade(
            address=pos.address, symbol=pos.symbol, name=pos.name, qty=pos.qty,
            entry_price=pos.entry_price, exit_price=fill,
            entry_value=pos.entry_value, exit_value=proceeds,
            pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 2),
            opened_at=pos.opened_at, closed_at=now(),
            entry_reason=pos.entry_reason, exit_reason=reason,
        )
        self.db.insert_trade(trade)
        del self.positions[address]
        self._persist()
        log.info("CLOSE %s pnl $%.2f (%.1f%%) — %s", pos.symbol, pnl, pnl_pct, reason)
        return trade

    # --- manual trading --------------------------------------------------- #
    def manual_buy(self, snap: TokenSnapshot, usd: float,
                   reason: str = "manual buy") -> Optional[Position]:
        """Buy a USD amount of a coin; averages into an existing position.
        Bypasses the auto-trade position cap (it's the user's explicit choice)."""
        usd = min(usd, self.cash * 0.999)
        if usd < 1 or snap.price_usd <= 0:
            return None
        fill = snap.price_usd * (1 + self.cfg.slippage_pct)
        qty = (usd * (1 - self.cfg.fee_pct)) / fill
        pos = self.positions.get(snap.address)
        if pos:                       # average in
            pos.qty += qty
            pos.entry_value += usd
            pos.entry_price = pos.entry_value / pos.qty if pos.qty else fill
            pos.last_price = snap.price_usd
            pos.peak_price = max(pos.peak_price, snap.price_usd)
        else:
            pos = Position(
                address=snap.address, symbol=snap.symbol, name=snap.name, url=snap.url,
                qty=qty, entry_price=fill, entry_value=usd, opened_at=now(),
                peak_price=fill, last_price=fill, entry_reason=reason)
            self.positions[snap.address] = pos
        self.cash -= usd
        self._persist()
        log.info("MANUAL BUY %s $%.0f @ %.8f", snap.symbol, usd, fill)
        return pos

    def manual_sell(self, address: str, price: float, fraction: float = 1.0,
                    reason: str = "manual sell") -> Optional[Trade]:
        """Sell a fraction (0-1) of a position at the given price."""
        pos = self.positions.get(address)
        if not pos:
            return None
        fraction = max(0.01, min(1.0, fraction))
        fill = max(0.0, price) * (1 - self.cfg.slippage_pct)
        qty_sold = pos.qty * fraction
        cost = pos.entry_value * fraction
        proceeds = qty_sold * fill * (1 - self.cfg.fee_pct)
        pnl = proceeds - cost
        pnl_pct = (pnl / cost * 100.0) if cost else 0.0
        self.cash += proceeds
        trade = Trade(
            address=pos.address, symbol=pos.symbol, name=pos.name, qty=qty_sold,
            entry_price=pos.entry_price, exit_price=fill, entry_value=cost,
            exit_value=proceeds, pnl=round(pnl, 2), pnl_pct=round(pnl_pct, 2),
            opened_at=pos.opened_at, closed_at=now(),
            entry_reason=pos.entry_reason, exit_reason=reason)
        self.db.insert_trade(trade)
        if fraction >= 0.999:
            del self.positions[address]
        else:
            pos.qty -= qty_sold
            pos.entry_value -= cost
        self._persist()
        log.info("MANUAL SELL %s %.0f%% pnl $%.2f", pos.symbol, fraction * 100, pnl)
        return trade

    # --- state ------------------------------------------------------------ #
    def portfolio_state(self) -> PortfolioState:
        stats = self.db.trade_stats()
        unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        return PortfolioState(
            cash=round(self.cash, 2),
            equity=round(self.equity(), 2),
            starting_balance=self.cfg.starting_balance_usd,
            realized_pnl=round(stats["realized"], 2),
            unrealized_pnl=round(unrealized, 2),
            open_positions=len(self.positions),
            total_trades=stats["n"],
            wins=stats["wins"], losses=stats["losses"],
        )

    def record_equity_point(self) -> None:
        st = self.portfolio_state()
        self.db.insert_equity(now(), st.equity, st.cash,
                              st.realized_pnl, st.unrealized_pnl)

    def _persist(self) -> None:
        self.db.kv_set("cash", self.cash)
        self.db.save_positions({a: asdict(p) for a, p in self.positions.items()})

    def reset(self) -> None:
        self.db.reset()
        self.cash = self.cfg.starting_balance_usd
        self.positions = {}
        self._persist()
