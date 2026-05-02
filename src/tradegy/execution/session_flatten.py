"""Session-boundary flatten.

Per `11_execution_layer_spec.md:264-281`. At each session end:

  1. Cancel all `WORKING` and `PARTIAL` orders for the instrument.
  2. For any non-flat position, submit a `MARKET` order with
     `tag = "session_end"`.
  3. After flatten confirmation, reset `intent_seq` for every strategy
     to 0.
  4. If session-end flatten cannot complete within 30 s, escalate to
     `UNKNOWN` and trigger the kill-switch.

This module computes WHAT to do at session-end (the cancel + flatten
intents). The dispatching of those orders is the live adapter's job
(Phase 3 wiring); the deadline-monitoring + kill-switch escalation is
the orchestration layer's job. Both are out of scope for this module —
we keep this pure so it can be tested without a clock or a broker.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from tradegy.execution.lifecycle import ManagedOrder, OrderState
from tradegy.strategies.types import Order, OrderType, Side


@dataclass(frozen=True)
class SessionFlattenPlan:
    """The cancel + flatten work needed at session-end, computed from
    the live order book + position book at the boundary instant.
    """

    cancels: tuple[ManagedOrder, ...]
    """ManagedOrders currently WORKING or PARTIAL that should be cancelled."""

    flatten_orders: tuple[Order, ...]
    """New MARKET orders to submit, one per non-flat position, side
    inverted to close. Tagged `session_end`."""

    @property
    def is_empty(self) -> bool:
        return not self.cancels and not self.flatten_orders


@dataclass(frozen=True)
class OpenPosition:
    """A non-flat position at the session boundary. Quantity is signed:
    positive = long, negative = short.
    """

    instrument: str
    quantity: int
    strategy_id: str = ""


_FLATTENABLE = frozenset({OrderState.WORKING, OrderState.PARTIAL})


def build_session_end_plan(
    *,
    open_orders: Iterable[ManagedOrder],
    open_positions: Iterable[OpenPosition],
) -> SessionFlattenPlan:
    """Compute the cancel + flatten orders for a single session boundary.

    Pure function: same inputs → same outputs. Caller dispatches the
    resulting orders to the live adapter and waits for confirmations.
    """
    cancels = tuple(
        o for o in open_orders if o.state in _FLATTENABLE
    )
    flatten_orders = tuple(
        Order(
            side=Side.SHORT if p.quantity > 0 else Side.LONG,
            type=OrderType.MARKET,
            quantity=abs(p.quantity),
            tag="session_end",
        )
        for p in open_positions
        if p.quantity != 0
    )
    return SessionFlattenPlan(
        cancels=cancels, flatten_orders=flatten_orders,
    )


def build_kill_switch_plan(
    *,
    open_orders: Iterable[ManagedOrder],
    open_positions: Iterable[OpenPosition],
) -> SessionFlattenPlan:
    """Same shape as the session-end plan, but tags flatten orders
    `kill_switch`. Per doc 11 §289-292.

    The cancellations are identical to session-end. The difference is
    purely the tag on the flatten orders, which propagates to ExitReason
    OVERRIDE in the closed trade record.
    """
    plan = build_session_end_plan(
        open_orders=open_orders, open_positions=open_positions,
    )
    relabeled = tuple(
        Order(
            side=o.side, type=o.type, quantity=o.quantity,
            limit_price=o.limit_price, stop_price=o.stop_price,
            tag="kill_switch",
        )
        for o in plan.flatten_orders
    )
    return SessionFlattenPlan(
        cancels=plan.cancels, flatten_orders=relabeled,
    )
