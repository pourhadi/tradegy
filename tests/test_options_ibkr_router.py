"""IbkrOptionsRouter tests.

Real chain data for the multi-leg input (per the no-synthetic-data
rule); MockIB for the broker side because we don't ship a unit-test
dependency on the live ib_async runtime.

Coverage:
  - place_combo turns a real MultiLegOrder + chain snapshot into a
    BAG contract with the right number of legs, ratios, and actions.
  - Net price computation: credit position → SELL action with
    positive limit; debit position → BUY action with positive limit.
  - Idempotency: re-placing the same client_order_id raises.
  - Cancel works against a tracked order and raises if not tracked.
  - get_combo returns the tracked ManagedOrder.
  - Health snapshot reports connection + tracked-combo count.
  - Subscriber notification fires on transition.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from tradegy.execution.ibkr_options_router import IbkrOptionsRouter
from tradegy.execution.lifecycle import OrderState
from tradegy.options.cost_model import OptionCostModel
from tradegy.options.runner import _open_position_from_order
from tradegy.options.strategies import (
    IronCondor45dteD16,
    PutCalendar30_60AtmDeb,
    PutCreditSpread45dteD30,
)


# ── MockIB ─────────────────────────────────────────────────────


@dataclass
class _MockOrderStatus:
    status: str = "Submitted"
    filled: int = 0


@dataclass
class _MockEvent:
    """Eventkit-style += subscription. Stores handlers as a list."""

    handlers: list = field(default_factory=list)

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, *args):
        for h in self.handlers:
            h(*args)


@dataclass
class _MockOrder:
    action: str
    totalQuantity: int
    lmtPrice: float
    orderRef: str = ""
    orderId: int = 0
    tif: str = "DAY"


@dataclass
class _MockTrade:
    contract: Any
    order: _MockOrder
    orderStatus: _MockOrderStatus = field(default_factory=_MockOrderStatus)
    statusEvent: _MockEvent = field(default_factory=_MockEvent)
    fillEvent: _MockEvent = field(default_factory=_MockEvent)


class _MockIB:
    """In-process IBKR shim. Gives qualifyContracts a deterministic
    conId and placeOrder a deterministic orderId.
    """

    def __init__(self) -> None:
        self._connected = True
        self._next_conid = 100_000_000
        self._next_orderid = 1
        self.placed: list[tuple[Any, Any]] = []
        self.cancelled: list[Any] = []

    def isConnected(self) -> bool:
        return self._connected

    def qualifyContracts(self, *contracts) -> list:
        out = []
        for c in contracts:
            # Stamp a synthetic conId; real IBKR fills it from the
            # contract resolver.
            c.conId = self._next_conid
            self._next_conid += 1
            out.append(c)
        return out

    async def qualifyContractsAsync(self, *contracts) -> list:
        # Mock async path mirrors the sync one — the real IB has
        # both and the router uses the async variant from inside
        # the runner's event loop.
        return self.qualifyContracts(*contracts)

    def placeOrder(self, contract, order) -> _MockTrade:
        order.orderId = self._next_orderid
        self._next_orderid += 1
        trade = _MockTrade(contract=contract, order=order)
        self.placed.append((contract, order))
        return trade

    def cancelOrder(self, order) -> None:
        self.cancelled.append(order)


# Stub the ib_async imports the router does lazily.
@pytest.fixture(autouse=True)
def _stub_ib_async(monkeypatch):
    """Replace ib_async.Option / Bag / ComboLeg / LimitOrder with
    plain dataclasses so the router can construct them without the
    live ib_async dependency. The real classes have similar shape
    (kwargs constructor + attribute access).
    """
    @dataclass
    class _StubOption:
        symbol: str = ""
        lastTradeDateOrContractMonth: str = ""
        strike: float = 0.0
        right: str = ""
        exchange: str = ""
        currency: str = ""
        tradingClass: str = ""
        conId: int = 0

    @dataclass
    class _StubComboLeg:
        conId: int = 0
        ratio: int = 0
        action: str = ""
        exchange: str = ""

    @dataclass
    class _StubBag:
        symbol: str = ""
        currency: str = ""
        exchange: str = ""
        comboLegs: list = field(default_factory=list)

    @dataclass
    class _StubLimitOrder:
        action: str = ""
        totalQuantity: int = 0
        lmtPrice: float = 0.0
        orderRef: str = ""
        orderId: int = 0
        tif: str = "DAY"

    import sys
    import types
    mod = types.ModuleType("ib_async")
    mod.Option = _StubOption
    mod.Bag = _StubBag
    mod.ComboLeg = _StubComboLeg
    mod.LimitOrder = _StubLimitOrder
    monkeypatch.setitem(sys.modules, "ib_async", mod)


# ── Tests ──────────────────────────────────────────────────────


def test_place_combo_constructs_bag_with_4_legs(real_spx_chain_snapshots):
    snap = real_spx_chain_snapshots[1]  # filling on snap[1]
    order = IronCondor45dteD16().on_chain(real_spx_chain_snapshots[0], ())
    assert order is not None and len(order.legs) == 4

    ib = _MockIB()
    router = IbkrOptionsRouter(ib=ib)

    import asyncio
    managed = asyncio.run(router.place_combo(
        order=order, snapshot=snap, client_order_id="ic_test_1",
    ))

    # MockIB recorded one placeOrder call.
    assert len(ib.placed) == 1
    bag, ib_order = ib.placed[0]
    # The BAG has 4 ComboLegs.
    assert len(bag.comboLegs) == 4
    # The order is a LimitOrder with positive lmtPrice.
    assert ib_order.lmtPrice > 0
    # ManagedOrder transitioned to SUBMITTED.
    assert managed.state == OrderState.SUBMITTED


def test_credit_position_is_sell_action(real_spx_chain_snapshots):
    """Iron condor is a credit position. The combo action should be
    SELL (we sell the combo as defined to receive net premium).
    """
    snap = real_spx_chain_snapshots[1]
    order = IronCondor45dteD16().on_chain(real_spx_chain_snapshots[0], ())
    ib = _MockIB()
    router = IbkrOptionsRouter(ib=ib)
    import asyncio
    asyncio.run(router.place_combo(
        order=order, snapshot=snap, client_order_id="credit_test",
    ))
    _, ib_order = ib.placed[0]
    assert ib_order.action == "SELL"


def test_debit_position_is_buy_action(real_spx_chain_snapshots):
    """Put calendar is a debit position. Combo action: BUY (we pay
    net premium to acquire the combo)."""
    snap = real_spx_chain_snapshots[1]
    order = PutCalendar30_60AtmDeb().on_chain(real_spx_chain_snapshots[0], ())
    ib = _MockIB()
    router = IbkrOptionsRouter(ib=ib)
    import asyncio
    asyncio.run(router.place_combo(
        order=order, snapshot=snap, client_order_id="debit_test",
    ))
    _, ib_order = ib.placed[0]
    assert ib_order.action == "BUY"


def test_combo_leg_ratios_match_quantity_magnitude(real_spx_chain_snapshots):
    """ComboLeg.ratio must equal abs(leg.quantity); leg.action is
    BUY for long (qty>0) and SELL for short (qty<0).
    """
    snap = real_spx_chain_snapshots[1]
    order = IronCondor45dteD16().on_chain(real_spx_chain_snapshots[0], ())
    ib = _MockIB()
    router = IbkrOptionsRouter(ib=ib)
    import asyncio
    asyncio.run(router.place_combo(
        order=order, snapshot=snap, client_order_id="ratios",
    ))
    bag, _ = ib.placed[0]
    for leg_order, combo_leg in zip(order.legs, bag.comboLegs):
        assert combo_leg.ratio == abs(leg_order.quantity)
        if leg_order.quantity > 0:
            assert combo_leg.action == "BUY"
        else:
            assert combo_leg.action == "SELL"


def test_idempotency_duplicate_coid_raises(real_spx_chain_snapshots):
    snap = real_spx_chain_snapshots[1]
    order = IronCondor45dteD16().on_chain(real_spx_chain_snapshots[0], ())
    ib = _MockIB()
    router = IbkrOptionsRouter(ib=ib)
    import asyncio
    asyncio.run(router.place_combo(
        order=order, snapshot=snap, client_order_id="dup",
    ))
    with pytest.raises(ValueError, match="already tracked"):
        asyncio.run(router.place_combo(
            order=order, snapshot=snap, client_order_id="dup",
        ))


def test_cancel_unknown_coid_raises(real_spx_chain_snapshots):
    ib = _MockIB()
    router = IbkrOptionsRouter(ib=ib)
    import asyncio
    with pytest.raises(KeyError, match="no Trade tracked"):
        asyncio.run(router.cancel_combo("nope"))


def test_cancel_known_coid_calls_broker(real_spx_chain_snapshots):
    snap = real_spx_chain_snapshots[1]
    order = IronCondor45dteD16().on_chain(real_spx_chain_snapshots[0], ())
    ib = _MockIB()
    router = IbkrOptionsRouter(ib=ib)
    import asyncio
    asyncio.run(router.place_combo(
        order=order, snapshot=snap, client_order_id="cancel_me",
    ))
    asyncio.run(router.cancel_combo("cancel_me"))
    assert len(ib.cancelled) == 1


def test_get_combo_returns_managed_order(real_spx_chain_snapshots):
    snap = real_spx_chain_snapshots[1]
    order = PutCreditSpread45dteD30().on_chain(real_spx_chain_snapshots[0], ())
    ib = _MockIB()
    router = IbkrOptionsRouter(ib=ib)
    import asyncio
    asyncio.run(router.place_combo(
        order=order, snapshot=snap, client_order_id="lookup",
    ))
    found = router.get_combo("lookup")
    assert found is not None
    assert found.client_order_id == "lookup"
    assert router.get_combo("nope") is None


def test_health_reports_tracked_count(real_spx_chain_snapshots):
    snap = real_spx_chain_snapshots[1]
    order = IronCondor45dteD16().on_chain(real_spx_chain_snapshots[0], ())
    ib = _MockIB()
    router = IbkrOptionsRouter(ib=ib)
    import asyncio
    health_before = router.health()
    assert health_before["tracked_combos"] == 0
    asyncio.run(router.place_combo(
        order=order, snapshot=snap, client_order_id="h",
    ))
    health_after = router.health()
    assert health_after["tracked_combos"] == 1
    assert health_after["connected"] is True


def test_subscriber_notified_on_submission(real_spx_chain_snapshots):
    snap = real_spx_chain_snapshots[1]
    order = IronCondor45dteD16().on_chain(real_spx_chain_snapshots[0], ())
    ib = _MockIB()
    router = IbkrOptionsRouter(ib=ib)
    notifications: list = []
    router.subscribe_transitions(
        lambda mo, t: notifications.append((mo.client_order_id, t.to_state)),
    )
    import asyncio
    asyncio.run(router.place_combo(
        order=order, snapshot=snap, client_order_id="notify",
    ))
    assert len(notifications) == 1
    coid, target = notifications[0]
    assert coid == "notify"
    assert target == OrderState.SUBMITTED
