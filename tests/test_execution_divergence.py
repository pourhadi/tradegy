"""Divergence detector tests.

Covers each row of the divergence resolution table in doc 11
§227-237. Pure-function tests — no clock, no broker.
"""
from __future__ import annotations

import pytest

from tradegy.execution.divergence import (
    DivergenceSeverity,
    DivergenceType,
    RecommendedAction,
    detect_account_divergences,
    detect_all_divergences,
    detect_order_divergences,
    detect_position_divergences,
)
from tradegy.execution.lifecycle import (
    OrderState,
    apply_transition,
    new_managed_order,
)
from tradegy.execution.lifecycle import TransitionSource
from tradegy.execution.router import (
    BrokerAccountState,
    BrokerOrderState,
    BrokerPosition,
)
from tradegy.strategies.types import Order, OrderType, Side


def _local_order(coid: str, state: OrderState, qty: int = 1, filled: int = 0):
    o = new_managed_order(
        client_order_id=coid,
        intent=Order(side=Side.LONG, type=OrderType.MARKET, quantity=qty, tag="t"),
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
            source=TransitionSource.BROKER, filled_quantity=filled or 1,
        )
    if state == OrderState.FILLED:
        return apply_transition(
            o, OrderState.FILLED,
            source=TransitionSource.BROKER, filled_quantity=qty,
        )
    if state == OrderState.CANCELLED:
        return apply_transition(o, OrderState.CANCELLED, source=TransitionSource.BROKER)
    raise NotImplementedError(state)


def _broker_order(coid: str, state: OrderState, filled: int = 0, total: int = 1):
    return BrokerOrderState(
        client_order_id=coid,
        broker_order_id="ib-1",
        state=state,
        filled_quantity=filled,
        remaining_quantity=max(0, total - filled),
    )


def _broker_position(instrument: str, qty: int) -> BrokerPosition:
    return BrokerPosition(instrument=instrument, quantity=qty, avg_cost=5000.0)


def _broker_account(
    *,
    available_funds: float = 50000.0,
    maintenance_margin: float = 5000.0,
    initial_margin: float = 6000.0,
) -> BrokerAccountState:
    return BrokerAccountState(
        available_funds=available_funds,
        net_liquidation=60000.0,
        initial_margin=initial_margin,
        maintenance_margin=maintenance_margin,
    )


# ── Order divergences ─────────────────────────────────────────────


def test_no_divergence_when_states_match():
    locals_ = {"a:1:0:e": _local_order("a:1:0:e", OrderState.WORKING)}
    brokers = [_broker_order("a:1:0:e", OrderState.WORKING)]
    out = detect_order_divergences(local_orders=locals_, broker_orders=brokers)
    assert out == []


def test_local_active_missing_at_broker_is_high():
    locals_ = {"a:1:0:e": _local_order("a:1:0:e", OrderState.WORKING)}
    out = detect_order_divergences(local_orders=locals_, broker_orders=[])
    assert len(out) == 1
    e = out[0]
    assert e.type == DivergenceType.ORDER_MISSING_AT_BROKER
    assert e.severity == DivergenceSeverity.HIGH
    assert e.action == RecommendedAction.MARK_LOCAL_UNKNOWN


def test_local_terminal_missing_at_broker_is_not_divergence():
    """Brokers drop terminal orders from openTrades, so a FILLED local
    order absent from broker.openTrades is expected, not a divergence.
    """
    locals_ = {"a:1:0:e": _local_order("a:1:0:e", OrderState.FILLED)}
    out = detect_order_divergences(local_orders=locals_, broker_orders=[])
    assert out == []


def test_local_filled_broker_working_is_critical():
    locals_ = {"a:1:0:e": _local_order("a:1:0:e", OrderState.FILLED)}
    brokers = [_broker_order("a:1:0:e", OrderState.WORKING)]
    out = detect_order_divergences(local_orders=locals_, broker_orders=brokers)
    assert len(out) == 1
    e = out[0]
    assert e.type == DivergenceType.LOCAL_FILLED_BROKER_WORKING
    assert e.severity == DivergenceSeverity.CRITICAL
    assert e.action == RecommendedAction.REOPEN_LOCAL_ORDER


def test_local_filled_broker_partial_is_also_critical():
    locals_ = {"a:1:0:e": _local_order("a:1:0:e", OrderState.FILLED)}
    brokers = [_broker_order("a:1:0:e", OrderState.PARTIAL)]
    out = detect_order_divergences(local_orders=locals_, broker_orders=brokers)
    assert len(out) == 1
    assert out[0].type == DivergenceType.LOCAL_FILLED_BROKER_WORKING


def test_broker_only_orders_not_reported():
    """Broker has an order we don't track locally — that's not a
    divergence the detector emits (we'd ingest it as UNKNOWN at start
    of session, but the detector itself doesn't fabricate local state).
    """
    brokers = [_broker_order("foreign:1:0:e", OrderState.WORKING)]
    out = detect_order_divergences(local_orders={}, broker_orders=brokers)
    assert out == []


# ── Position divergences ──────────────────────────────────────────


def test_no_position_divergence_when_matched():
    out = detect_position_divergences(
        local_positions={"MES": 2},
        broker_positions=[_broker_position("MES", 2)],
    )
    assert out == []


def test_local_long_broker_flat_is_critical():
    out = detect_position_divergences(
        local_positions={"MES": 2}, broker_positions=[],
    )
    assert len(out) == 1
    e = out[0]
    assert e.type == DivergenceType.POSITION_LOCAL_BROKER_FLAT
    assert e.severity == DivergenceSeverity.CRITICAL
    assert e.action == RecommendedAction.FLATTEN_LOCAL


def test_local_flat_broker_long_is_critical():
    out = detect_position_divergences(
        local_positions={},
        broker_positions=[_broker_position("MES", 1)],
    )
    assert len(out) == 1
    e = out[0]
    assert e.type == DivergenceType.POSITION_LOCAL_FLAT_BROKER_OPEN
    assert e.severity == DivergenceSeverity.CRITICAL
    assert e.action == RecommendedAction.FLATTEN_BROKER


def test_quantity_mismatch_is_critical():
    out = detect_position_divergences(
        local_positions={"MES": 1},
        broker_positions=[_broker_position("MES", 3)],
    )
    assert len(out) == 1
    e = out[0]
    assert e.type == DivergenceType.POSITION_QUANTITY_MISMATCH
    assert e.action == RecommendedAction.FLATTEN_BROKER


def test_signed_quantity_mismatch_is_detected():
    """Local short, broker long — same instrument, opposite signs."""
    out = detect_position_divergences(
        local_positions={"MES": -1},
        broker_positions=[_broker_position("MES", +1)],
    )
    assert len(out) == 1
    assert out[0].type == DivergenceType.POSITION_QUANTITY_MISMATCH


def test_zero_local_zero_broker_is_no_divergence():
    out = detect_position_divergences(
        local_positions={"MES": 0},
        broker_positions=[_broker_position("MES", 0)],
    )
    assert out == []


# ── Account divergences ──────────────────────────────────────────


def test_no_margin_breach_when_funds_exceed_margin():
    a = _broker_account(available_funds=50000.0, maintenance_margin=5000.0)
    out = detect_account_divergences(broker_account=a)
    assert out == []


def test_margin_breach_is_critical():
    a = _broker_account(available_funds=1000.0, maintenance_margin=5000.0)
    out = detect_account_divergences(broker_account=a)
    assert len(out) == 1
    e = out[0]
    assert e.type == DivergenceType.MARGIN_BREACH
    assert e.severity == DivergenceSeverity.CRITICAL
    assert e.action == RecommendedAction.BLOCK_NEW_ORDERS_AND_FLATTEN


def test_zero_maintenance_margin_skips_check():
    """Maintenance margin = 0 means the broker hasn't reported a
    value yet (or there's no open exposure). Skip rather than flag.
    """
    a = _broker_account(available_funds=0.0, maintenance_margin=0.0)
    out = detect_account_divergences(broker_account=a)
    assert out == []


# ── Composite ─────────────────────────────────────────────────────


def test_detect_all_returns_in_spec_order():
    locals_orders = {"a:1:0:e": _local_order("a:1:0:e", OrderState.WORKING)}
    out = detect_all_divergences(
        local_orders=locals_orders,
        broker_orders=[],
        local_positions={"MES": 1},
        broker_positions=[],
        broker_account=_broker_account(
            available_funds=1.0, maintenance_margin=100.0,
        ),
    )
    # Order divergence first, then position, then account.
    types = [e.type for e in out]
    assert types == [
        DivergenceType.ORDER_MISSING_AT_BROKER,
        DivergenceType.POSITION_LOCAL_BROKER_FLAT,
        DivergenceType.MARGIN_BREACH,
    ]


def test_detect_all_account_optional():
    out = detect_all_divergences(
        local_orders={}, broker_orders=[],
        local_positions={}, broker_positions=[],
        broker_account=None,
    )
    assert out == []
