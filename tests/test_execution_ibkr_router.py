"""IBKROrderRouter tests using a deterministic MockIB.

The MockIB is a small in-memory stand-in for ib_async.IB. It exposes
just the methods the router calls (placeOrder, cancelOrder, isConnected,
openTrades, positions, accountSummary) and lets tests fire status events
synthetically. No real broker connection is required.

When the integration tests run against a paper IBKR account (deferred
to user-run), the same router code drives a real ib_async.IB instance.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from tradegy.execution.ibkr_router import IBKROrderRouter
from tradegy.execution.lifecycle import OrderState
from tradegy.strategies.types import Order, OrderType, Side


# ── Mock IB ─────────────────────────────────────────────────────────


class MockEvent:
    """Stand-in for ib_async eventkit Event. Supports `+=` to add a
    handler and a `.fire(*args)` method to invoke them all.
    """

    def __init__(self) -> None:
        self._handlers: list = []

    def __iadd__(self, handler):
        self._handlers.append(handler)
        return self

    def fire(self, *args, **kwargs) -> None:
        for h in list(self._handlers):
            h(*args, **kwargs)


@dataclass
class MockOrderStatus:
    status: str = "PendingSubmit"
    filled: int = 0


@dataclass
class MockIBOrder:
    action: str
    totalQuantity: int
    orderType: str = "MKT"
    orderRef: str = ""
    orderId: int = 0
    tif: str = "GTC"


@dataclass
class MockTrade:
    """ib_async Trade-like — has order, orderStatus, statusEvent, fillEvent."""

    order: MockIBOrder
    contract: Any
    orderStatus: MockOrderStatus = field(default_factory=MockOrderStatus)
    statusEvent: MockEvent = field(default_factory=MockEvent)
    fillEvent: MockEvent = field(default_factory=MockEvent)


@dataclass
class MockContract:
    symbol: str
    conId: int = 12345


@dataclass
class MockPosition:
    contract: MockContract
    position: int
    avgCost: float


@dataclass
class MockAccountValue:
    tag: str
    value: str


class MockIB:
    """Minimum viable IBLike for router unit tests."""

    def __init__(self) -> None:
        self._connected = True
        self._next_order_id = 1000
        self._open_trades: list[MockTrade] = []
        self._positions: list[MockPosition] = []
        self._account: dict[str, str] = {
            "AvailableFunds": "50000.0",
            "NetLiquidation": "60000.0",
            "InitMarginReq": "5000.0",
            "MaintMarginReq": "4000.0",
        }

    def isConnected(self) -> bool:
        return self._connected

    def placeOrder(self, contract, order: MockIBOrder) -> MockTrade:
        order.orderId = self._next_order_id
        self._next_order_id += 1
        trade = MockTrade(order=order, contract=contract)
        self._open_trades.append(trade)
        return trade

    def cancelOrder(self, order: MockIBOrder) -> None:
        # In real IB this dispatches a cancel request; the resolution
        # arrives via statusEvent. Tests fire the event explicitly.
        pass

    def openTrades(self) -> list[MockTrade]:
        return list(self._open_trades)

    def positions(self) -> list[MockPosition]:
        return list(self._positions)

    def accountSummary(self, account: str = "") -> list[MockAccountValue]:
        return [MockAccountValue(tag=k, value=v) for k, v in self._account.items()]


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def mock_ib() -> MockIB:
    return MockIB()


@pytest.fixture
def router(mock_ib: MockIB) -> IBKROrderRouter:
    def resolver(instrument: str) -> MockContract:
        return MockContract(symbol=instrument)

    return IBKROrderRouter(ib=mock_ib, contract_resolver=resolver)


def _market_buy(qty: int = 1) -> Order:
    return Order(
        side=Side.LONG, type=OrderType.MARKET, quantity=qty,
        tag="strategy:entry",
    )


def _stop_short(stop_price: float, qty: int = 1) -> Order:
    return Order(
        side=Side.SHORT, type=OrderType.STOP, quantity=qty,
        stop_price=stop_price, tag="strategy:stop",
    )


def _await(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── connect / health ───────────────────────────────────────────────


def test_connect_succeeds_when_ib_connected(router):
    _await(router.connect())  # no exception


def test_connect_raises_when_ib_disconnected(mock_ib, router):
    mock_ib._connected = False
    with pytest.raises(RuntimeError, match="not connected"):
        _await(router.connect())


def test_health_reports_state(router):
    h = router.health()
    assert h["connected"] is True
    assert h["tracked_orders"] == 0


# ── place ──────────────────────────────────────────────────────────


def test_place_records_managed_order_in_submitted(router):
    intent = _market_buy()
    managed = _await(
        router.place(
            intent=intent,
            client_order_id="demo:20260501:0:entry",
            instrument="MES",
        )
    )
    assert managed.state == OrderState.SUBMITTED
    assert managed.client_order_id == "demo:20260501:0:entry"
    assert managed.broker_order_id is not None
    # 2 transitions: PENDING (initial) + SUBMITTED.
    assert len(managed.transitions) == 2


def test_place_stamps_order_ref_on_ib_order(mock_ib, router):
    _await(
        router.place(
            intent=_market_buy(),
            client_order_id="demo:20260501:0:entry",
            instrument="MES",
        )
    )
    [trade] = mock_ib._open_trades
    assert trade.order.orderRef == "demo:20260501:0:entry"
    assert trade.order.tif == "GTC"


def test_duplicate_client_order_id_rejected(router):
    _await(
        router.place(
            intent=_market_buy(),
            client_order_id="x:1:0:entry",
            instrument="MES",
        )
    )
    with pytest.raises(ValueError, match="already tracked"):
        _await(
            router.place(
                intent=_market_buy(),
                client_order_id="x:1:0:entry",
                instrument="MES",
            )
        )


def test_stop_order_passes_stop_price(mock_ib, router):
    _await(
        router.place(
            intent=_stop_short(5000.0),
            client_order_id="x:1:1:stop",
            instrument="MES",
        )
    )
    [trade] = mock_ib._open_trades
    # The MockIB doesn't store StopOrder fields explicitly, but the
    # call must not have raised.
    assert trade.order.action == "SELL"
    assert trade.order.totalQuantity == 1


def test_stop_order_without_price_raises(router):
    bad = Order(
        side=Side.SHORT, type=OrderType.STOP, quantity=1, tag="bad",
    )
    with pytest.raises(ValueError, match="stop_price"):
        _await(
            router.place(
                intent=bad,
                client_order_id="x:1:1:stop",
                instrument="MES",
            )
        )


# ── status events ──────────────────────────────────────────────────


def test_status_event_drives_pending_to_working(mock_ib, router):
    managed = _await(
        router.place(
            intent=_market_buy(),
            client_order_id="x:1:0:entry",
            instrument="MES",
        )
    )
    assert managed.state == OrderState.SUBMITTED

    [trade] = mock_ib._open_trades
    trade.orderStatus.status = "Submitted"
    trade.statusEvent.fire(trade)

    after = router.get_order("x:1:0:entry")
    assert after.state == OrderState.WORKING


def test_status_event_drives_filled_terminal(mock_ib, router):
    _await(
        router.place(
            intent=_market_buy(qty=2),
            client_order_id="x:1:0:entry",
            instrument="MES",
        )
    )
    [trade] = mock_ib._open_trades

    trade.orderStatus.status = "Submitted"
    trade.statusEvent.fire(trade)

    trade.orderStatus.status = "Filled"
    trade.orderStatus.filled = 2
    trade.statusEvent.fire(trade)

    after = router.get_order("x:1:0:entry")
    assert after.state == OrderState.FILLED
    assert after.filled_quantity == 2
    assert after.is_terminal


def test_partial_fill_then_full(mock_ib, router):
    _await(
        router.place(
            intent=_market_buy(qty=10),
            client_order_id="x:1:0:entry",
            instrument="MES",
        )
    )
    [trade] = mock_ib._open_trades

    trade.orderStatus.status = "Submitted"
    trade.statusEvent.fire(trade)
    assert router.get_order("x:1:0:entry").state == OrderState.WORKING

    trade.orderStatus.status = "Filled"
    trade.orderStatus.filled = 4
    trade.statusEvent.fire(trade)
    p = router.get_order("x:1:0:entry")
    assert p.state == OrderState.PARTIAL
    assert p.filled_quantity == 4

    trade.orderStatus.filled = 10
    trade.statusEvent.fire(trade)
    f = router.get_order("x:1:0:entry")
    assert f.state == OrderState.FILLED
    assert f.filled_quantity == 10


def test_idempotent_status_event_does_not_re_transition(mock_ib, router):
    _await(
        router.place(
            intent=_market_buy(),
            client_order_id="x:1:0:entry",
            instrument="MES",
        )
    )
    [trade] = mock_ib._open_trades
    trade.orderStatus.status = "Submitted"
    trade.statusEvent.fire(trade)
    pre = router.get_order("x:1:0:entry")
    transitions_before = len(pre.transitions)
    # Fire the same status again — nothing should change.
    trade.statusEvent.fire(trade)
    post = router.get_order("x:1:0:entry")
    assert len(post.transitions) == transitions_before


def test_terminal_status_ignores_subsequent_events(mock_ib, router):
    _await(
        router.place(
            intent=_market_buy(qty=1),
            client_order_id="x:1:0:entry",
            instrument="MES",
        )
    )
    [trade] = mock_ib._open_trades
    trade.orderStatus.status = "Submitted"
    trade.statusEvent.fire(trade)
    trade.orderStatus.status = "Filled"
    trade.orderStatus.filled = 1
    trade.statusEvent.fire(trade)
    pre = router.get_order("x:1:0:entry")
    assert pre.is_terminal
    transitions_before = len(pre.transitions)
    # Late-arriving status event after terminal — must be ignored.
    trade.orderStatus.status = "Submitted"
    trade.statusEvent.fire(trade)
    post = router.get_order("x:1:0:entry")
    assert len(post.transitions) == transitions_before


def test_pre_flight_reject_event_drives_rejected(mock_ib, router):
    _await(
        router.place(
            intent=_market_buy(),
            client_order_id="x:1:0:entry",
            instrument="MES",
        )
    )
    [trade] = mock_ib._open_trades
    trade.orderStatus.status = "Inactive"
    trade.statusEvent.fire(trade)
    assert router.get_order("x:1:0:entry").state == OrderState.REJECTED


# ── transition handlers ────────────────────────────────────────────


def test_subscribe_transitions_fires_on_each_change(mock_ib, router):
    received = []
    router.subscribe_transitions(
        lambda mo, rec: received.append((mo.state, rec.to_state))
    )
    _await(
        router.place(
            intent=_market_buy(),
            client_order_id="x:1:0:entry",
            instrument="MES",
        )
    )
    [trade] = mock_ib._open_trades
    trade.orderStatus.status = "Submitted"
    trade.statusEvent.fire(trade)
    trade.orderStatus.status = "Filled"
    trade.orderStatus.filled = 1
    trade.statusEvent.fire(trade)

    states = [r[1] for r in received]
    assert OrderState.SUBMITTED in states
    assert OrderState.WORKING in states
    assert OrderState.FILLED in states


def test_handler_exception_does_not_break_router(mock_ib, router):
    def bad_handler(mo, rec):
        raise RuntimeError("boom")

    router.subscribe_transitions(bad_handler)
    # Should not raise.
    _await(
        router.place(
            intent=_market_buy(),
            client_order_id="x:1:0:entry",
            instrument="MES",
        )
    )


# ── cancel ─────────────────────────────────────────────────────────


def test_cancel_dispatches_to_ib(mock_ib, router):
    cancel_calls = []
    real_cancel = mock_ib.cancelOrder
    mock_ib.cancelOrder = lambda o: cancel_calls.append(o) or real_cancel(o)

    _await(
        router.place(
            intent=_market_buy(),
            client_order_id="x:1:0:entry",
            instrument="MES",
        )
    )
    _await(router.cancel("x:1:0:entry"))
    assert len(cancel_calls) == 1


def test_cancel_unknown_coid_raises(router):
    with pytest.raises(KeyError):
        _await(router.cancel("never:placed"))


# ── query methods ──────────────────────────────────────────────────


def test_query_open_orders_returns_broker_view(mock_ib, router):
    _await(
        router.place(
            intent=_market_buy(qty=2),
            client_order_id="x:1:0:entry",
            instrument="MES",
        )
    )
    [trade] = mock_ib._open_trades
    trade.orderStatus.status = "Submitted"
    trade.orderStatus.filled = 0

    out = _await(router.query_open_orders())
    assert len(out) == 1
    [bo] = out
    assert bo.client_order_id == "x:1:0:entry"
    assert bo.state == OrderState.WORKING
    assert bo.filled_quantity == 0
    assert bo.remaining_quantity == 2


def test_query_positions_returns_signed_quantities(mock_ib, router):
    mock_ib._positions = [
        MockPosition(MockContract("MES"), +3, avgCost=5000.0),
        MockPosition(MockContract("MNQ"), -1, avgCost=20000.0),
    ]
    out = _await(router.query_positions())
    qmap = {p.instrument: p.quantity for p in out}
    assert qmap == {"MES": 3, "MNQ": -1}


def test_query_account_extracts_required_fields(router):
    a = _await(router.query_account())
    assert a.available_funds == 50000.0
    assert a.net_liquidation == 60000.0
    assert a.initial_margin == 5000.0
    assert a.maintenance_margin == 4000.0
