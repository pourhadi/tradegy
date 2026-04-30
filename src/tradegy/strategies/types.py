"""Core dataclasses passed between the harness and registered strategy
classes (and auxiliary classes).

These types are deliberately minimal — every field has to earn its place
because the harness, every strategy class, every sizing / stop / exit
class, and the live execution layer all consume them. Adding a field is
contagious; removing one breaks every consumer.

All money / risk amounts are R-multiples or contract counts — never
account-currency floats. Price levels are the instrument's native price
units (ES points, etc.).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class ExitReason(str, Enum):
    TARGET = "target"
    STOP = "stop"
    INVALIDATION = "invalidation"
    TIME = "time"
    SESSION_END = "session_end"
    OVERRIDE = "override"


@dataclass(frozen=True)
class Bar:
    """Single OHLCV bar passed to a strategy's on_bar."""

    ts_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class FeatureSnapshot:
    """All registered feature values available to the strategy at a bar.

    Keys are feature ids; values are the latest published value at-or-
    before the current bar's ts_utc, with the source feature's
    availability_latency already applied at retrieval time.
    """

    ts_utc: datetime
    values: dict[str, float]

    def get(self, feature_id: str, default: float | None = None) -> float | None:
        return self.values.get(feature_id, default)


@dataclass
class Order:
    """Order intent emitted by a strategy class. The execution layer
    translates this into a fill (subject to slippage / cost model)."""

    side: Side
    type: OrderType
    quantity: int
    limit_price: float | None = None
    stop_price: float | None = None
    tag: str = ""  # human-readable origin marker for the trade log


@dataclass(frozen=True)
class Fill:
    """Confirmed execution of an order."""

    ts_utc: datetime
    side: Side
    quantity: int
    price: float
    slippage_ticks: float
    commission: float
    tag: str = ""


@dataclass
class Position:
    """Open position state. Quantity is signed (positive = long, negative
    = short) for arithmetic convenience."""

    quantity: int = 0
    avg_entry_price: float = 0.0
    initial_stop_price: float | None = None
    current_stop_price: float | None = None
    entry_ts: datetime | None = None
    bars_since_entry: int = 0

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0

    @property
    def side(self) -> Side | None:
        if self.quantity > 0:
            return Side.LONG
        if self.quantity < 0:
            return Side.SHORT
        return None


@dataclass
class State:
    """Per-session state container handed to a strategy class on every
    callback. Strategy classes own this object's contents (the harness
    just holds the reference)."""

    instrument: str
    session_date: datetime
    parameters: dict[str, Any]
    position: Position = field(default_factory=Position)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Trade:
    """Closed round-trip record produced by the harness."""

    trade_id: str
    strategy_id: str
    instrument: str
    entry_ts: datetime
    exit_ts: datetime
    side: Side
    quantity: int
    entry_price: float
    exit_price: float
    initial_stop_price: float
    initial_risk_ticks: float
    gross_pnl: float
    commissions: float
    slippage_cost: float
    net_pnl: float
    net_pnl_R: float
    holding_bars: int
    exit_reason: ExitReason
