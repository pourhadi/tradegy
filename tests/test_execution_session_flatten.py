"""Session-flatten tests.

build_session_end_plan + build_kill_switch_plan are pure functions that
compute the cancel + flatten work given the open orders + open positions
at a session boundary. Tests cover order-state filtering, side-flip on
flatten, and tag differences between the two plans.
"""
from __future__ import annotations

from datetime import datetime, timezone

from tradegy.execution.lifecycle import (
    OrderState,
    apply_transition,
    new_managed_order,
)
from tradegy.execution.lifecycle import TransitionSource
from tradegy.execution.session_flatten import (
    OpenPosition,
    build_kill_switch_plan,
    build_session_end_plan,
)
from tradegy.strategies.types import Order, OrderType, Side


def _make_order_in_state(coid: str, state: OrderState):
    o = new_managed_order(
        client_order_id=coid,
        intent=Order(side=Side.LONG, type=OrderType.MARKET, quantity=1, tag="x"),
    )
    if state == OrderState.PENDING:
        return o
    o = apply_transition(o, OrderState.SUBMITTED, source=TransitionSource.LOCAL)
    if state == OrderState.SUBMITTED:
        return o
    o = apply_transition(o, OrderState.WORKING, source=TransitionSource.BROKER)
    if state == OrderState.WORKING:
        return o
    if state == OrderState.PARTIAL:
        return apply_transition(
            o, OrderState.PARTIAL,
            source=TransitionSource.BROKER, filled_quantity=0,
        )
    raise NotImplementedError(state)


def test_empty_inputs_produce_empty_plan():
    plan = build_session_end_plan(open_orders=[], open_positions=[])
    assert plan.is_empty


def test_only_working_and_partial_are_cancelled():
    pending = _make_order_in_state("a:1:0:entry", OrderState.PENDING)
    submitted = _make_order_in_state("a:1:1:entry", OrderState.SUBMITTED)
    working = _make_order_in_state("a:1:2:entry", OrderState.WORKING)
    partial = _make_order_in_state("a:1:3:entry", OrderState.PARTIAL)
    plan = build_session_end_plan(
        open_orders=[pending, submitted, working, partial],
        open_positions=[],
    )
    cancelled_ids = {o.client_order_id for o in plan.cancels}
    assert cancelled_ids == {"a:1:2:entry", "a:1:3:entry"}


def test_long_position_flattens_short():
    pos = OpenPosition(instrument="MES", quantity=+2, strategy_id="demo")
    plan = build_session_end_plan(open_orders=[], open_positions=[pos])
    assert len(plan.flatten_orders) == 1
    fo = plan.flatten_orders[0]
    assert fo.side == Side.SHORT
    assert fo.quantity == 2
    assert fo.type == OrderType.MARKET
    assert fo.tag == "session_end"


def test_short_position_flattens_long():
    pos = OpenPosition(instrument="MES", quantity=-3, strategy_id="demo")
    plan = build_session_end_plan(open_orders=[], open_positions=[pos])
    fo = plan.flatten_orders[0]
    assert fo.side == Side.LONG
    assert fo.quantity == 3
    assert fo.tag == "session_end"


def test_flat_position_yields_no_flatten_order():
    pos = OpenPosition(instrument="MES", quantity=0, strategy_id="demo")
    plan = build_session_end_plan(open_orders=[], open_positions=[pos])
    assert plan.flatten_orders == ()


def test_kill_switch_plan_uses_kill_switch_tag():
    pos = OpenPosition(instrument="MES", quantity=+1, strategy_id="demo")
    working = _make_order_in_state("a:1:2:entry", OrderState.WORKING)
    plan = build_kill_switch_plan(
        open_orders=[working], open_positions=[pos],
    )
    assert len(plan.cancels) == 1
    assert plan.flatten_orders[0].tag == "kill_switch"


def test_kill_switch_and_session_end_cancels_match():
    working = _make_order_in_state("a:1:2:entry", OrderState.WORKING)
    s = build_session_end_plan(open_orders=[working], open_positions=[])
    k = build_kill_switch_plan(open_orders=[working], open_positions=[])
    assert s.cancels == k.cancels


def test_multiple_positions_produce_multiple_flatten_orders():
    plan = build_session_end_plan(
        open_orders=[],
        open_positions=[
            OpenPosition("MES", +1, "a"),
            OpenPosition("MNQ", -2, "b"),
            OpenPosition("ES", 0, "c"),  # flat — ignored
        ],
    )
    assert len(plan.flatten_orders) == 2
    sides = sorted(fo.side.value for fo in plan.flatten_orders)
    assert sides == ["long", "short"]
