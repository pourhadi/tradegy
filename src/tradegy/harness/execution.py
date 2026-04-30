"""Simulated execution layer for the backtest harness.

Per 05_backtest_harness.md:118-148, every order incurs modeled slippage
and commission. The MVP supports market and stop fills only — limit
fills (require price to trade through, no slippage) are deferred until
a strategy spec needs them.

Slippage model: fixed-tick per-side, configurable per instrument. Simple
and crude; the docs explicitly accept this for v1.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tradegy.strategies.types import (
    Bar,
    Fill,
    Order,
    OrderType,
    Position,
    Side,
)


@dataclass
class CostModel:
    """Per-instrument execution cost parameters."""

    tick_size: float = 0.25
    slippage_ticks_per_side: float = 0.5  # adverse to the trader
    commission_per_contract_round_trip: float = 1.50

    @property
    def commission_per_side(self) -> float:
        return self.commission_per_contract_round_trip / 2.0


def fill_market_order(
    order: Order, next_bar_open: float, ts_utc: datetime, cost: CostModel
) -> Fill:
    """Fill a market order at the next bar's open + adverse slippage."""
    slip = cost.slippage_ticks_per_side * cost.tick_size
    if order.side == Side.LONG:
        price = next_bar_open + slip
    else:
        price = next_bar_open - slip
    return Fill(
        ts_utc=ts_utc,
        side=order.side,
        quantity=order.quantity,
        price=price,
        slippage_ticks=cost.slippage_ticks_per_side,
        commission=cost.commission_per_side * order.quantity,
        tag=order.tag,
    )


def fill_stop_at_price(
    side_to_close: Side,
    quantity: int,
    stop_price: float,
    ts_utc: datetime,
    cost: CostModel,
    tag: str = "stop_fill",
) -> Fill:
    """Fill a triggered stop at the stop price + adverse slippage."""
    slip = cost.slippage_ticks_per_side * cost.tick_size
    # Closing a long: SELL → adverse slip is below stop.
    # Closing a short: BUY → adverse slip is above stop.
    if side_to_close == Side.LONG:
        # closing side is SHORT (sell), adverse fill is below the stop
        fill_price = stop_price - slip
        closing_side = Side.SHORT
    else:
        fill_price = stop_price + slip
        closing_side = Side.LONG
    return Fill(
        ts_utc=ts_utc,
        side=closing_side,
        quantity=quantity,
        price=fill_price,
        slippage_ticks=cost.slippage_ticks_per_side,
        commission=cost.commission_per_side * quantity,
        tag=tag,
    )


def stop_triggered_during_bar(
    position: Position, bar: Bar
) -> bool:
    """Did the bar's range trade through the stop?"""
    if position.is_flat or position.current_stop_price is None:
        return False
    stop = position.current_stop_price
    if position.side == Side.LONG:
        return bar.low <= stop
    return bar.high >= stop


def apply_fill(position: Position, fill: Fill) -> int:
    """Update Position in place with a fill. Returns the realized PnL
    contribution in price units (positive = profit). Useful for trade
    record assembly.
    """
    if position.is_flat:
        # Opening trade.
        signed_qty = fill.quantity if fill.side == Side.LONG else -fill.quantity
        position.quantity = signed_qty
        position.avg_entry_price = fill.price
        position.entry_ts = fill.ts_utc
        position.bars_since_entry = 0
        return 0
    # Closing trade — assume same quantity (no partial fills in MVP).
    closed_qty = abs(position.quantity)
    entry_px = position.avg_entry_price
    side_at_open = position.side
    if side_at_open == Side.LONG:
        per_contract_pnl = fill.price - entry_px
    else:
        per_contract_pnl = entry_px - fill.price
    realized = per_contract_pnl * closed_qty
    position.quantity = 0
    position.avg_entry_price = 0.0
    position.initial_stop_price = None
    position.current_stop_price = None
    position.entry_ts = None
    position.bars_since_entry = 0
    return realized
