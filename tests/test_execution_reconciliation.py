"""ReconciliationLoop tests.

The loop's `tick(now)` is the unit of test — it inspects the injected
`now` against last-run timestamps and decides which checks fire.
Production's `run_forever` is just a thin asyncio.sleep wrapper around
tick; one happy-path test exercises that path with `max_ticks` to
keep it deterministic.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from tradegy.execution.divergence import (
    DivergenceSeverity,
    DivergenceType,
)
from tradegy.execution.lifecycle import (
    OrderState,
    apply_transition,
    new_managed_order,
)
from tradegy.execution.lifecycle import TransitionSource
from tradegy.execution.reconciliation import (
    DEFAULT_CADENCES,
    CheckType,
    ReconciliationLoop,
)
from tradegy.execution.router import (
    BrokerAccountState,
    BrokerOrderState,
    BrokerPosition,
    BrokerRouter,
)
from tradegy.strategies.types import Order, OrderType, Side


# ── Mock router ────────────────────────────────────────────────────


class MockRouter(BrokerRouter):
    """Minimal stand-in. Tests fill `_open_orders`, `_positions`,
    `_account` to control what each query returns; counters track
    how many times each method was called.
    """

    def __init__(self):
        self._open_orders: list[BrokerOrderState] = []
        self._positions: list[BrokerPosition] = []
        self._account: BrokerAccountState = BrokerAccountState(
            available_funds=50000.0,
            net_liquidation=60000.0,
            initial_margin=1000.0,
            maintenance_margin=500.0,
        )
        self.calls = {"orders": 0, "positions": 0, "account": 0}

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def place(self, **kwargs): raise NotImplementedError
    async def cancel(self, client_order_id: str) -> None: ...

    async def query_open_orders(self):
        self.calls["orders"] += 1
        return list(self._open_orders)

    async def query_positions(self):
        self.calls["positions"] += 1
        return list(self._positions)

    async def query_account(self):
        self.calls["account"] += 1
        return self._account

    def subscribe_transitions(self, handler) -> None: ...
    def health(self) -> dict[str, Any]: return {}
    def get_order(self, coid): return None


def _local_order(coid: str, state: OrderState):
    o = new_managed_order(
        client_order_id=coid,
        intent=Order(side=Side.LONG, type=OrderType.MARKET, quantity=1, tag="t"),
    )
    if state == OrderState.PENDING:
        return o
    o = apply_transition(o, OrderState.SUBMITTED, source=TransitionSource.LOCAL)
    if state == OrderState.SUBMITTED:
        return o
    o = apply_transition(o, OrderState.WORKING, source=TransitionSource.BROKER)
    return o


def _ts(seconds_offset: float = 0) -> datetime:
    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=seconds_offset)


def _await(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def captured_events() -> list:
    return []


@pytest.fixture
def captured_escalations() -> list:
    return []


@pytest.fixture
def loop_factory(captured_events, captured_escalations):
    def factory(
        router: MockRouter,
        local_orders=None,
        local_positions=None,
        cadences=None,
    ):
        def provider():
            return (
                dict(local_orders or {}),
                dict(local_positions or {}),
            )
        return ReconciliationLoop(
            router=router,
            local_state_provider=provider,
            divergence_handler=lambda e: captured_events.append(e),
            escalation_handler=lambda e: captured_escalations.append(e),
            cadences=cadences,
        )
    return factory


# ── First tick runs every check ───────────────────────────────────


def test_first_tick_runs_every_check(loop_factory):
    router = MockRouter()
    loop = loop_factory(router)
    report = _await(loop.tick(now=_ts()))
    # All four cadences are due on first tick (last_run is None).
    assert set(report.checks_run) == {
        CheckType.OPEN_ORDERS,
        CheckType.POSITIONS,
        CheckType.ACCOUNT,
        CheckType.PNL,
    }
    assert router.calls == {"orders": 1, "positions": 1, "account": 1}


# ── Cadence respects timestamps ──────────────────────────────────


def test_within_cadence_skips_checks(loop_factory):
    router = MockRouter()
    loop = loop_factory(router)
    _await(loop.tick(now=_ts(0)))
    # 0.5s later — nothing is due yet (open-orders cadence is 1s).
    report = _await(loop.tick(now=_ts(0.5)))
    assert report.checks_run == []


def test_open_orders_runs_at_1s(loop_factory):
    router = MockRouter()
    loop = loop_factory(router)
    _await(loop.tick(now=_ts(0)))
    report = _await(loop.tick(now=_ts(1.0)))
    assert CheckType.OPEN_ORDERS in report.checks_run
    assert CheckType.POSITIONS not in report.checks_run  # 5s cadence


def test_positions_runs_at_5s(loop_factory):
    router = MockRouter()
    loop = loop_factory(router)
    _await(loop.tick(now=_ts(0)))
    _await(loop.tick(now=_ts(1.5)))  # only orders run
    report = _await(loop.tick(now=_ts(5.5)))
    assert CheckType.POSITIONS in report.checks_run


def test_account_runs_at_30s(loop_factory):
    router = MockRouter()
    loop = loop_factory(router)
    _await(loop.tick(now=_ts(0)))
    report = _await(loop.tick(now=_ts(30.0)))
    assert CheckType.ACCOUNT in report.checks_run


def test_pnl_runs_at_60s(loop_factory):
    router = MockRouter()
    loop = loop_factory(router)
    _await(loop.tick(now=_ts(0)))
    report = _await(loop.tick(now=_ts(60.0)))
    assert CheckType.PNL in report.checks_run


def test_custom_cadences_honored(loop_factory):
    router = MockRouter()
    loop = loop_factory(
        router,
        cadences={
            CheckType.OPEN_ORDERS: 2.0,
            CheckType.POSITIONS: 10.0,
            CheckType.ACCOUNT: 60.0,
            CheckType.PNL: 120.0,
        },
    )
    _await(loop.tick(now=_ts(0)))
    report = _await(loop.tick(now=_ts(1.0)))  # default 1s would fire; custom 2s does not.
    assert CheckType.OPEN_ORDERS not in report.checks_run


# ── Divergence dispatch ──────────────────────────────────────────


def test_divergence_dispatched_to_handler(loop_factory, captured_events):
    router = MockRouter()
    # Local has WORKING order; broker has nothing → ORDER_MISSING_AT_BROKER (HIGH).
    loop = loop_factory(
        router,
        local_orders={"a:1:0:e": _local_order("a:1:0:e", OrderState.WORKING)},
    )
    _await(loop.tick(now=_ts()))
    assert any(
        e.type == DivergenceType.ORDER_MISSING_AT_BROKER
        for e in captured_events
    )


def test_critical_event_escalated(
    loop_factory, captured_events, captured_escalations,
):
    router = MockRouter()
    # Local position 1, broker flat → POSITION_LOCAL_BROKER_FLAT (CRITICAL).
    loop = loop_factory(
        router,
        local_positions={"MES": 1},
    )
    _await(loop.tick(now=_ts()))
    # Both handlers see the event; escalation is the CRITICAL one only.
    assert any(
        e.severity == DivergenceSeverity.CRITICAL for e in captured_events
    )
    assert len(captured_escalations) >= 1
    assert all(
        e.severity == DivergenceSeverity.CRITICAL for e in captured_escalations
    )


def test_high_event_not_escalated(
    loop_factory, captured_events, captured_escalations,
):
    router = MockRouter()
    # ORDER_MISSING_AT_BROKER is HIGH, not CRITICAL.
    loop = loop_factory(
        router,
        local_orders={"a:1:0:e": _local_order("a:1:0:e", OrderState.WORKING)},
    )
    _await(loop.tick(now=_ts()))
    assert any(
        e.severity == DivergenceSeverity.HIGH for e in captured_events
    )
    # No HIGH-only events should escalate.
    assert all(
        e.severity == DivergenceSeverity.CRITICAL
        for e in captured_escalations
    )


def test_handler_exception_does_not_break_loop(loop_factory, captured_events):
    router = MockRouter()

    raise_count = {"divergence": 0, "escalation": 0}

    def bad_divergence(e):
        raise_count["divergence"] += 1
        raise RuntimeError("boom")

    def bad_escalation(e):
        raise_count["escalation"] += 1
        raise RuntimeError("escalation boom")

    loop = ReconciliationLoop(
        router=router,
        local_state_provider=lambda: (
            {"a:1:0:e": _local_order("a:1:0:e", OrderState.WORKING)},
            {"MES": 1},
        ),
        divergence_handler=bad_divergence,
        escalation_handler=bad_escalation,
    )
    # Must not raise.
    report = _await(loop.tick(now=_ts()))
    assert raise_count["divergence"] >= 1
    assert raise_count["escalation"] >= 1
    assert len(report.events) >= 2


# ── run_forever ──────────────────────────────────────────────────


def test_run_forever_runs_max_ticks(loop_factory):
    router = MockRouter()
    loop = loop_factory(router)
    _await(loop.run_forever(sleep_seconds=0.001, max_ticks=3))
    # Each tick on first iteration calls all four; subsequent depend on
    # time, but with sleep 1ms most stay un-due. The first tick guarantees
    # >= 1 call to each.
    assert router.calls["orders"] >= 1
    assert router.calls["positions"] >= 1
    assert router.calls["account"] >= 1


def test_run_forever_continues_through_tick_exception(loop_factory, monkeypatch):
    router = MockRouter()
    loop = loop_factory(router)

    boom_count = {"n": 0}
    real_tick = loop.tick

    async def bad_tick(*, now):
        boom_count["n"] += 1
        if boom_count["n"] == 1:
            raise RuntimeError("tick failure")
        return await real_tick(now=now)

    loop.tick = bad_tick  # type: ignore[assignment]
    _await(loop.run_forever(sleep_seconds=0.001, max_ticks=3))
    assert boom_count["n"] == 3  # didn't crash; kept going.


# ── Local state provider ─────────────────────────────────────────


def test_local_state_provider_called_each_tick(loop_factory):
    router = MockRouter()
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return ({}, {})

    loop = ReconciliationLoop(
        router=router,
        local_state_provider=provider,
        divergence_handler=lambda e: None,
        escalation_handler=lambda e: None,
    )
    _await(loop.tick(now=_ts(0)))
    # First tick runs orders + positions, both call the provider.
    assert calls["n"] >= 2
