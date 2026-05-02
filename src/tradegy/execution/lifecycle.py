"""Order lifecycle state machine.

Per `11_execution_layer_spec.md:62-100`. Every order moves through this
FSM. The `(state, transition_log)` pair is sufficient to reconstruct an
order's full history — the log is append-only and replayable.

The transition graph (doc 11 §80-95):

    PENDING   ──► SUBMITTED ──► WORKING ──► FILLED
                                      │
                                      ├──► PARTIAL ──► FILLED
                                      │              │
                                      │              └──► CANCELLED
                                      ├──► CANCELLED
                                      └──► EXPIRED

    PENDING   ──► REJECTED   (pre-flight fail; never sent)
    SUBMITTED ──► REJECTED   (broker rejects on receipt)
    ANY       ──► UNKNOWN    (broker silent past timeout; reconciliation owns)
    UNKNOWN   ──► WORKING|FILLED|CANCELLED|REJECTED  (reconciliation resolves)

`apply_transition` is a pure function: it validates the transition is
legal, returns a new ManagedOrder with the updated state and an
appended transition record. Illegal transitions raise.

This module is intentionally broker-agnostic. The live IBKR adapter
(out-of-scope here) consumes broker events and calls `apply_transition`
with `source=TransitionSource.BROKER`.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum

from tradegy.strategies.types import Order


class OrderState(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    WORKING = "working"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


TERMINAL_STATES: frozenset[OrderState] = frozenset({
    OrderState.FILLED,
    OrderState.CANCELLED,
    OrderState.REJECTED,
    OrderState.EXPIRED,
})


# Per doc 11 §80-95. Each entry: from_state -> set of legal to_states.
# UNKNOWN can be reached from any non-terminal state via the
# reconciliation timeout path; we encode that as ANY -> UNKNOWN below
# rather than enumerating every from-state explicitly.
LEGAL_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.PENDING: frozenset({
        OrderState.SUBMITTED,
        OrderState.REJECTED,
        OrderState.UNKNOWN,
    }),
    OrderState.SUBMITTED: frozenset({
        OrderState.WORKING,
        OrderState.REJECTED,
        OrderState.FILLED,    # immediate fill on a marketable order
        OrderState.CANCELLED, # pre-acknowledgment cancel race
        OrderState.UNKNOWN,
    }),
    OrderState.WORKING: frozenset({
        OrderState.PARTIAL,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.UNKNOWN,
    }),
    OrderState.PARTIAL: frozenset({
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
        OrderState.UNKNOWN,
    }),
    OrderState.UNKNOWN: frozenset({
        OrderState.WORKING,
        OrderState.PARTIAL,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
    }),
    # Terminal states have no outbound transitions.
    OrderState.FILLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.REJECTED: frozenset(),
    OrderState.EXPIRED: frozenset(),
}


class TransitionSource(str, Enum):
    """Per doc 11 §97-99: who originated the transition."""

    LOCAL = "local"
    BROKER = "broker"
    RECONCILIATION = "reconciliation"
    OPERATOR = "operator"


class IllegalTransition(Exception):
    """Raised when `apply_transition` is asked for a state change that
    the FSM does not allow. The exception message names both states so
    the caller can fix the bug; the FSM never silently corrects.
    """

    def __init__(
        self, from_state: OrderState, to_state: OrderState, reason: str = ""
    ) -> None:
        self.from_state = from_state
        self.to_state = to_state
        msg = f"illegal transition {from_state.value} → {to_state.value}"
        if reason:
            msg += f" ({reason})"
        super().__init__(msg)


@dataclass(frozen=True)
class TransitionRecord:
    """One entry in the order's append-only transition log.

    Per doc 11 §97-99: `(order_id, from_state, to_state, ts_utc, reason,
    source)`.
    """

    order_id: str  # client_order_id
    from_state: OrderState
    to_state: OrderState
    ts_utc: datetime
    source: TransitionSource
    reason: str = ""
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ManagedOrder:
    """The execution-layer-owned record for a live order.

    Wraps a strategy-emitted `Order` with lifecycle state, idempotency
    key, and an append-only transition history. The strategy never
    mutates a ManagedOrder directly; only `apply_transition` and the
    initial constructor produce them.
    """

    client_order_id: str
    intent: Order  # the strategy's original Order intent
    state: OrderState
    created_at: datetime
    last_transition_at: datetime
    filled_quantity: int = 0
    transitions: tuple[TransitionRecord, ...] = ()
    broker_order_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def remaining_quantity(self) -> int:
        return max(0, self.intent.quantity - self.filled_quantity)


def new_managed_order(
    *,
    client_order_id: str,
    intent: Order,
    now: datetime | None = None,
) -> ManagedOrder:
    """Create a fresh ManagedOrder in PENDING state with an initial
    transition record.

    The initial record is the system-of-record for "this order was
    born"; downstream callers see a non-empty transition log on every
    ManagedOrder they observe.
    """
    ts = now if now is not None else datetime.now(tz=timezone.utc)
    initial = TransitionRecord(
        order_id=client_order_id,
        from_state=OrderState.PENDING,
        to_state=OrderState.PENDING,
        ts_utc=ts,
        source=TransitionSource.LOCAL,
        reason="created",
    )
    return ManagedOrder(
        client_order_id=client_order_id,
        intent=intent,
        state=OrderState.PENDING,
        created_at=ts,
        last_transition_at=ts,
        filled_quantity=0,
        transitions=(initial,),
    )


def apply_transition(
    order: ManagedOrder,
    to_state: OrderState,
    *,
    source: TransitionSource,
    ts_utc: datetime | None = None,
    reason: str = "",
    filled_quantity: int | None = None,
    broker_order_id: str | None = None,
    detail: dict | None = None,
) -> ManagedOrder:
    """Apply a state transition to a ManagedOrder.

    Returns a new ManagedOrder with the updated state, appended
    transition record, and any side-channel updates (cumulative fill
    quantity, broker_order_id). Raises `IllegalTransition` for any
    transition the FSM does not allow.

    Self-transitions (state == to_state) are allowed only for PARTIAL,
    where successive partial fills append a record without changing
    the state field but advancing `filled_quantity`.
    """
    ts = ts_utc if ts_utc is not None else datetime.now(tz=timezone.utc)
    same_state = order.state == to_state

    if same_state and to_state != OrderState.PARTIAL:
        raise IllegalTransition(
            order.state, to_state,
            reason="self-transition only allowed for PARTIAL fill updates",
        )
    if not same_state:
        legal = LEGAL_TRANSITIONS.get(order.state, frozenset())
        if to_state not in legal:
            raise IllegalTransition(order.state, to_state)

    new_filled = (
        order.filled_quantity if filled_quantity is None else filled_quantity
    )
    if new_filled > order.intent.quantity:
        raise IllegalTransition(
            order.state, to_state,
            reason=(
                f"filled_quantity {new_filled} exceeds intent quantity "
                f"{order.intent.quantity}"
            ),
        )
    if to_state == OrderState.FILLED and new_filled != order.intent.quantity:
        raise IllegalTransition(
            order.state, to_state,
            reason=(
                f"FILLED requires filled_quantity == intent quantity "
                f"({new_filled} != {order.intent.quantity})"
            ),
        )

    record = TransitionRecord(
        order_id=order.client_order_id,
        from_state=order.state,
        to_state=to_state,
        ts_utc=ts,
        source=source,
        reason=reason,
        detail=dict(detail or {}),
    )
    return replace(
        order,
        state=to_state,
        last_transition_at=ts,
        filled_quantity=new_filled,
        transitions=order.transitions + (record,),
        broker_order_id=(
            broker_order_id
            if broker_order_id is not None
            else order.broker_order_id
        ),
    )
