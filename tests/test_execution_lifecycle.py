"""Order lifecycle FSM tests.

Per `11_execution_layer_spec.md:62-100`. The state machine is the
foundation of the live execution layer; every legal/illegal transition
documented in the spec needs an explicit test so future refactors
don't silently widen the legal set.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tradegy.execution.lifecycle import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    IllegalTransition,
    OrderState,
    TransitionSource,
    apply_transition,
    new_managed_order,
)
from tradegy.strategies.types import Order, OrderType, Side


def _make_order(qty: int = 1) -> Order:
    return Order(
        side=Side.LONG, type=OrderType.MARKET, quantity=qty, tag="test:entry"
    )


def _ts(seconds: int = 0) -> datetime:
    return datetime(2026, 5, 1, 12, 0, seconds, tzinfo=timezone.utc)


def test_initial_state_is_pending_with_one_record():
    o = new_managed_order(
        client_order_id="demo:20260501:0:entry",
        intent=_make_order(),
        now=_ts(),
    )
    assert o.state == OrderState.PENDING
    assert len(o.transitions) == 1
    assert o.transitions[0].reason == "created"
    assert o.transitions[0].source == TransitionSource.LOCAL
    assert o.filled_quantity == 0
    assert not o.is_terminal


def test_terminal_states_match_spec():
    assert TERMINAL_STATES == {
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    }


def test_terminal_states_have_no_outbound_transitions():
    for s in TERMINAL_STATES:
        assert LEGAL_TRANSITIONS[s] == frozenset()


def test_pending_to_submitted_to_working_to_filled_happy_path():
    o = new_managed_order(
        client_order_id="x:20260501:0:entry",
        intent=_make_order(qty=2),
        now=_ts(),
    )
    o = apply_transition(
        o, OrderState.SUBMITTED, source=TransitionSource.LOCAL, ts_utc=_ts(1)
    )
    o = apply_transition(
        o, OrderState.WORKING, source=TransitionSource.BROKER, ts_utc=_ts(2)
    )
    o = apply_transition(
        o, OrderState.FILLED,
        source=TransitionSource.BROKER, ts_utc=_ts(3), filled_quantity=2,
    )
    assert o.state == OrderState.FILLED
    assert o.is_terminal
    assert o.filled_quantity == 2
    assert len(o.transitions) == 4  # 1 initial + 3 transitions


def test_partial_fill_path():
    o = new_managed_order(
        client_order_id="x:20260501:0:entry",
        intent=_make_order(qty=10),
        now=_ts(),
    )
    o = apply_transition(o, OrderState.SUBMITTED, source=TransitionSource.LOCAL)
    o = apply_transition(o, OrderState.WORKING, source=TransitionSource.BROKER)
    o = apply_transition(
        o, OrderState.PARTIAL,
        source=TransitionSource.BROKER, filled_quantity=3,
    )
    # Successive partial fills are self-transitions on PARTIAL.
    o = apply_transition(
        o, OrderState.PARTIAL,
        source=TransitionSource.BROKER, filled_quantity=7,
    )
    assert o.state == OrderState.PARTIAL
    assert o.filled_quantity == 7
    assert o.remaining_quantity == 3
    o = apply_transition(
        o, OrderState.FILLED,
        source=TransitionSource.BROKER, filled_quantity=10,
    )
    assert o.state == OrderState.FILLED
    assert o.remaining_quantity == 0


def test_pending_to_rejected_preflight():
    o = new_managed_order(
        client_order_id="x:20260501:0:entry",
        intent=_make_order(),
    )
    o = apply_transition(
        o, OrderState.REJECTED,
        source=TransitionSource.LOCAL, reason="margin_check_failed",
    )
    assert o.state == OrderState.REJECTED
    assert o.is_terminal


def test_submitted_to_rejected_broker():
    o = new_managed_order(
        client_order_id="x:20260501:0:entry",
        intent=_make_order(),
    )
    o = apply_transition(o, OrderState.SUBMITTED, source=TransitionSource.LOCAL)
    o = apply_transition(
        o, OrderState.REJECTED,
        source=TransitionSource.BROKER, reason="invalid_contract",
    )
    assert o.state == OrderState.REJECTED


def test_unknown_transition_from_working_resolves_via_reconciliation():
    o = new_managed_order(
        client_order_id="x:20260501:0:entry",
        intent=_make_order(),
    )
    o = apply_transition(o, OrderState.SUBMITTED, source=TransitionSource.LOCAL)
    o = apply_transition(o, OrderState.WORKING, source=TransitionSource.BROKER)
    o = apply_transition(
        o, OrderState.UNKNOWN,
        source=TransitionSource.LOCAL, reason="broker_silent_past_timeout",
    )
    # Reconciliation resolves it.
    o = apply_transition(
        o, OrderState.FILLED,
        source=TransitionSource.RECONCILIATION,
        filled_quantity=1,
    )
    assert o.state == OrderState.FILLED


def test_self_transition_on_non_partial_state_is_illegal():
    o = new_managed_order(
        client_order_id="x:20260501:0:entry",
        intent=_make_order(),
    )
    with pytest.raises(IllegalTransition):
        apply_transition(o, OrderState.PENDING, source=TransitionSource.LOCAL)


def test_terminal_state_is_truly_terminal():
    o = new_managed_order(
        client_order_id="x:20260501:0:entry",
        intent=_make_order(),
    )
    o = apply_transition(o, OrderState.SUBMITTED, source=TransitionSource.LOCAL)
    o = apply_transition(o, OrderState.WORKING, source=TransitionSource.BROKER)
    o = apply_transition(
        o, OrderState.FILLED,
        source=TransitionSource.BROKER, filled_quantity=1,
    )
    with pytest.raises(IllegalTransition):
        apply_transition(o, OrderState.WORKING, source=TransitionSource.BROKER)


def test_illegal_skip_pending_to_working():
    o = new_managed_order(
        client_order_id="x:20260501:0:entry",
        intent=_make_order(),
    )
    with pytest.raises(IllegalTransition):
        apply_transition(o, OrderState.WORKING, source=TransitionSource.BROKER)


def test_filled_quantity_cannot_exceed_intent():
    o = new_managed_order(
        client_order_id="x:20260501:0:entry",
        intent=_make_order(qty=5),
    )
    o = apply_transition(o, OrderState.SUBMITTED, source=TransitionSource.LOCAL)
    o = apply_transition(o, OrderState.WORKING, source=TransitionSource.BROKER)
    with pytest.raises(IllegalTransition):
        apply_transition(
            o, OrderState.PARTIAL,
            source=TransitionSource.BROKER, filled_quantity=6,
        )


def test_filled_requires_full_quantity():
    o = new_managed_order(
        client_order_id="x:20260501:0:entry",
        intent=_make_order(qty=5),
    )
    o = apply_transition(o, OrderState.SUBMITTED, source=TransitionSource.LOCAL)
    o = apply_transition(o, OrderState.WORKING, source=TransitionSource.BROKER)
    with pytest.raises(IllegalTransition):
        apply_transition(
            o, OrderState.FILLED,
            source=TransitionSource.BROKER, filled_quantity=4,
        )


def test_transitions_are_immutable_appends():
    o = new_managed_order(
        client_order_id="x:20260501:0:entry",
        intent=_make_order(),
    )
    initial_transitions = o.transitions
    o2 = apply_transition(o, OrderState.SUBMITTED, source=TransitionSource.LOCAL)
    # Original ManagedOrder unchanged.
    assert o.transitions is initial_transitions
    assert len(o.transitions) == 1
    assert len(o2.transitions) == 2


def test_broker_order_id_stickiness():
    o = new_managed_order(
        client_order_id="x:20260501:0:entry",
        intent=_make_order(),
    )
    o = apply_transition(
        o, OrderState.SUBMITTED, source=TransitionSource.LOCAL,
    )
    o = apply_transition(
        o, OrderState.WORKING,
        source=TransitionSource.BROKER, broker_order_id="IB-12345",
    )
    assert o.broker_order_id == "IB-12345"
    # Later transitions don't lose it.
    o = apply_transition(
        o, OrderState.FILLED,
        source=TransitionSource.BROKER, filled_quantity=1,
    )
    assert o.broker_order_id == "IB-12345"
