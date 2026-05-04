"""Tests for the V3a fill-confirmation poller.

Pure-async tests using a duck-typed router. We're testing OUR
poll-loop behavior, not testing the IBKR connection — the router's
state field is what we care about, and we can simulate its
transitions deterministically.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from tradegy.execution.lifecycle import OrderState
from tradegy.live.options_routing import await_terminal_state


@dataclass
class _FakeManagedOrder:
    state: OrderState


class _FakeRouter:
    """Duck-types `router.get_combo(coid).state`. Tests drive
    state transitions by mutating the underlying ManagedOrder.
    """
    def __init__(self) -> None:
        self._orders: dict[str, _FakeManagedOrder] = {}

    def get_combo(self, coid: str):
        return self._orders.get(coid)

    def add(self, coid: str, state: OrderState) -> None:
        self._orders[coid] = _FakeManagedOrder(state=state)

    def transition(self, coid: str, state: OrderState) -> None:
        self._orders[coid].state = state


def test_returns_immediately_on_already_terminal():
    """If the order is already FILLED when we start polling,
    return immediately without sleeping out the timeout.
    """
    router = _FakeRouter()
    router.add("coid_1", OrderState.FILLED)
    state = asyncio.run(await_terminal_state(
        router=router, client_order_id="coid_1",
        timeout_seconds=10.0, poll_interval_seconds=0.01,
    ))
    assert state == OrderState.FILLED


def test_returns_terminal_when_state_changes_mid_poll():
    """Order starts WORKING; transitions to FILLED while we're
    polling. We pick it up on the next poll tick.
    """
    router = _FakeRouter()
    router.add("coid_2", OrderState.WORKING)

    async def transition_after_delay():
        await asyncio.sleep(0.05)
        router.transition("coid_2", OrderState.FILLED)

    async def runner():
        task = asyncio.create_task(transition_after_delay())
        state = await await_terminal_state(
            router=router, client_order_id="coid_2",
            timeout_seconds=2.0, poll_interval_seconds=0.01,
        )
        await task
        return state

    state = asyncio.run(runner())
    assert state == OrderState.FILLED


def test_returns_non_terminal_state_on_timeout():
    """Order stays WORKING throughout; poller times out and
    returns the current (non-terminal) state.
    """
    router = _FakeRouter()
    router.add("coid_3", OrderState.WORKING)
    state = asyncio.run(await_terminal_state(
        router=router, client_order_id="coid_3",
        timeout_seconds=0.1, poll_interval_seconds=0.01,
    ))
    assert state == OrderState.WORKING


def test_returns_unknown_when_order_not_tracked():
    """Coid not in router → returns UNKNOWN. Defensive: shouldn't
    happen because place_combo always tracks the coid before
    returning, but the poller shouldn't crash.
    """
    router = _FakeRouter()
    state = asyncio.run(await_terminal_state(
        router=router, client_order_id="missing",
        timeout_seconds=0.05, poll_interval_seconds=0.01,
    ))
    assert state == OrderState.UNKNOWN


def test_handles_rejected_terminal_state():
    """REJECTED is terminal — poller returns it immediately."""
    router = _FakeRouter()
    router.add("coid_5", OrderState.REJECTED)
    state = asyncio.run(await_terminal_state(
        router=router, client_order_id="coid_5",
        timeout_seconds=10.0, poll_interval_seconds=0.01,
    ))
    assert state == OrderState.REJECTED


def test_handles_cancelled_terminal_state():
    router = _FakeRouter()
    router.add("coid_6", OrderState.CANCELLED)
    state = asyncio.run(await_terminal_state(
        router=router, client_order_id="coid_6",
        timeout_seconds=10.0, poll_interval_seconds=0.01,
    ))
    assert state == OrderState.CANCELLED
