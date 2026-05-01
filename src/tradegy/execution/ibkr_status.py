"""IBKR order-status string → tradegy OrderState mapping.

IBKR's `OrderStatus.status` is a free-text string with ~10 values.
ib_async surfaces them on `Trade.orderStatus.status`. This module is
the single source of truth for mapping each value into our internal
FSM state.

Mapping rationale (per IBKR's TWS API docs and ib_async source):

  IBKR status        → tradegy OrderState
  ---------------------------------------
  PendingSubmit      → SUBMITTED          (we just sent it; broker hasn't ACK'd)
  PendingCancel      → keep current state  (cancel pending; final state arrives next event)
  PreSubmitted       → SUBMITTED          (broker received but routing not done)
  Submitted          → WORKING            (in the order book)
  ApiPending         → SUBMITTED
  ApiCancelled       → CANCELLED          (cancelled before transmit)
  Cancelled          → CANCELLED
  Filled             → FILLED | PARTIAL   (depends on filled vs total qty)
  Inactive           → REJECTED           (broker considers it dead)

Defensive policy: any unrecognized status returns UNKNOWN with a
warning; the FSM then routes it through the reconciliation path. We
NEVER guess — better to escalate than to silently drop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from tradegy.execution.lifecycle import OrderState


_log = logging.getLogger(__name__)


# Statuses where the FSM state is unambiguous.
_DIRECT_MAP: dict[str, OrderState] = {
    "PendingSubmit": OrderState.SUBMITTED,
    "PreSubmitted": OrderState.SUBMITTED,
    "ApiPending": OrderState.SUBMITTED,
    "Submitted": OrderState.WORKING,
    "ApiCancelled": OrderState.CANCELLED,
    "Cancelled": OrderState.CANCELLED,
    "Inactive": OrderState.REJECTED,
}


# Statuses where the cumulative fill quantity disambiguates the state.
_FILL_DEPENDENT = frozenset({"Filled"})


# Statuses that are non-terminal "wait for the next event" markers.
# We hold the local state unchanged; the resolution event arrives next.
_HOLD = frozenset({"PendingCancel"})


@dataclass(frozen=True)
class StatusMapping:
    """Result of mapping one IBKR event onto a candidate FSM state.

    `target_state` is None if the caller should hold the current state
    (e.g., PendingCancel — wait for the resolution event).

    `is_terminal_hint` is True when the broker considers the order
    settled; the caller can clean up resources after applying.
    """

    target_state: OrderState | None
    is_terminal_hint: bool
    note: str = ""


def map_ibkr_status(
    status: str, *, filled: int, total: int
) -> StatusMapping:
    """Translate an IBKR status string + fill counts into a target
    OrderState.

    `filled` is the cumulative filled quantity reported by the broker;
    `total` is the order's intended total quantity.
    """
    if status in _HOLD:
        return StatusMapping(target_state=None, is_terminal_hint=False, note="hold")

    if status in _DIRECT_MAP:
        target = _DIRECT_MAP[status]
        return StatusMapping(
            target_state=target,
            is_terminal_hint=target in (
                OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED, OrderState.FILLED,
            ),
            note=f"direct:{status}",
        )

    if status in _FILL_DEPENDENT:
        if total <= 0:
            return StatusMapping(
                target_state=OrderState.UNKNOWN, is_terminal_hint=False,
                note=f"Filled with total={total}, cannot disambiguate",
            )
        if filled >= total:
            return StatusMapping(
                target_state=OrderState.FILLED, is_terminal_hint=True,
                note=f"Filled (full: {filled}/{total})",
            )
        if filled > 0:
            return StatusMapping(
                target_state=OrderState.PARTIAL, is_terminal_hint=False,
                note=f"Filled (partial: {filled}/{total})",
            )
        # Filled status with zero filled qty is anomalous; escalate.
        return StatusMapping(
            target_state=OrderState.UNKNOWN, is_terminal_hint=False,
            note=f"Filled with filled=0, total={total} — anomalous",
        )

    _log.warning(
        "map_ibkr_status: unrecognized IBKR status %r (filled=%d/%d); routing to UNKNOWN",
        status, filled, total,
    )
    return StatusMapping(
        target_state=OrderState.UNKNOWN, is_terminal_hint=False,
        note=f"unrecognized:{status}",
    )
