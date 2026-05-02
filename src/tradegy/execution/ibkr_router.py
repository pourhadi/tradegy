"""IBKR order router — wires the lifecycle FSM to ib_async.

Per `11_execution_layer_spec.md` Phase 3A. The router translates between
two worlds:

  Tradegy side                     IBKR / ib_async side
  ────────────                     ────────────────────
  Order intent (Side, type, qty)   ib_async.MarketOrder / StopOrder
  client_order_id (idempotency)    Order.orderRef (free-text tag)
  apply_transition(...)            trade.statusEvent / trade.fillEvent
  ManagedOrder map                 ib.openTrades() snapshot

The IB client is dependency-injected. Tests pass a mock; production
passes a real `ib_async.IB`. The router never imports the live module
directly so the live module's connection lifecycle stays a separate
concern.

Phase 3A scope: place / cancel / query_* + event handling. The
reconciliation loop that periodically calls the query_* methods and
detects divergence is Phase 3B (separate module).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol

from tradegy.execution.ibkr_status import map_ibkr_status
from tradegy.execution.lifecycle import (
    ManagedOrder,
    OrderState,
    TransitionSource,
    apply_transition,
    new_managed_order,
)
from tradegy.execution.router import (
    BrokerAccountState,
    BrokerOrderState,
    BrokerPosition,
    BrokerRouter,
    TransitionHandler,
)
from tradegy.strategies.types import Order, OrderType, Side


_log = logging.getLogger(__name__)


class _IBLike(Protocol):
    """The slice of ib_async.IB the router actually uses. Letting the
    router depend on this Protocol (and not the full IB class) makes
    test mocking straightforward.
    """

    def isConnected(self) -> bool: ...
    def placeOrder(self, contract, order) -> Any: ...
    def cancelOrder(self, order) -> None: ...
    def openTrades(self) -> list: ...
    def positions(self) -> list: ...
    def accountSummary(self, account: str = "") -> list: ...


# Type alias for the contract-resolver callable. Production wiring uses
# `IBKRConnection.qualify_contract`; tests can pass a lambda returning
# a stub object.
ContractResolver = Callable[[str], Any]


class IBKROrderRouter(BrokerRouter):
    """ib_async-backed order router.

    Tracks ManagedOrders in-memory keyed by client_order_id. Each
    successful `place` subscribes to the underlying Trade's status +
    fill events; event handlers translate IBKR statuses via
    `map_ibkr_status` and call `apply_transition` to advance the FSM.
    """

    def __init__(
        self,
        *,
        ib: _IBLike,
        contract_resolver: ContractResolver,
    ) -> None:
        self._ib = ib
        self._resolve_contract = contract_resolver
        self._orders: dict[str, ManagedOrder] = {}
        self._trades: dict[str, Any] = {}  # coid → ib_async Trade
        self._handlers: list[TransitionHandler] = []
        # Cache of qualified contracts keyed by instrument string.
        self._contract_cache: dict[str, Any] = {}

    # ── lifecycle ─────────────────────────────────────────────────

    async def connect(self) -> None:
        # Connection itself is owned by IBKRConnection (live module);
        # the router only checks connectivity.
        if not self._ib.isConnected():
            raise RuntimeError(
                "IBKROrderRouter: IB client not connected — call "
                "IBKRConnection.connect() before constructing the router"
            )

    async def disconnect(self) -> None:
        # Clear cached state. The IB connection itself is owned externally.
        self._orders.clear()
        self._trades.clear()
        self._contract_cache.clear()

    # ── public API ────────────────────────────────────────────────

    async def place(
        self,
        *,
        intent: Order,
        client_order_id: str,
        instrument: str,
        ts_utc: datetime | None = None,
    ) -> ManagedOrder:
        if client_order_id in self._orders:
            raise ValueError(
                f"IBKROrderRouter.place: client_order_id "
                f"{client_order_id!r} already tracked; idempotency "
                "violation upstream"
            )

        ts = ts_utc if ts_utc is not None else datetime.now(tz=timezone.utc)
        managed = new_managed_order(
            client_order_id=client_order_id, intent=intent, now=ts,
        )

        contract = self._get_contract(instrument)
        ib_order = self._build_ib_order(intent, client_order_id)
        trade = self._ib.placeOrder(contract, ib_order)

        # Wire event handlers BEFORE we transition — ib_async fires
        # events synchronously on placeOrder if the broker has already
        # acknowledged.
        self._wire_trade_events(client_order_id, trade)

        # Locally we've sent it; record the SUBMITTED transition.
        managed = apply_transition(
            managed, OrderState.SUBMITTED,
            source=TransitionSource.LOCAL, ts_utc=ts,
            reason="placeOrder dispatched", broker_order_id=str(
                getattr(getattr(trade, "order", None), "orderId", "")
            ) or None,
        )
        self._orders[client_order_id] = managed
        self._trades[client_order_id] = trade
        self._notify(managed, managed.transitions[-1])
        return managed

    async def cancel(self, client_order_id: str) -> None:
        trade = self._trades.get(client_order_id)
        if trade is None:
            raise KeyError(
                f"IBKROrderRouter.cancel: no Trade tracked for "
                f"{client_order_id!r}"
            )
        self._ib.cancelOrder(trade.order)
        # State transition arrives via the status event; nothing else to do.

    async def query_open_orders(self) -> list[BrokerOrderState]:
        out: list[BrokerOrderState] = []
        for trade in self._ib.openTrades():
            order = trade.order
            status = trade.orderStatus
            coid = getattr(order, "orderRef", "") or ""
            mapping = map_ibkr_status(
                status.status,
                filled=int(getattr(status, "filled", 0) or 0),
                total=int(getattr(order, "totalQuantity", 0) or 0),
            )
            target = mapping.target_state or OrderState.UNKNOWN
            filled = int(getattr(status, "filled", 0) or 0)
            total = int(getattr(order, "totalQuantity", 0) or 0)
            out.append(
                BrokerOrderState(
                    client_order_id=coid,
                    broker_order_id=str(getattr(order, "orderId", "")),
                    state=target,
                    filled_quantity=filled,
                    remaining_quantity=max(0, total - filled),
                )
            )
        return out

    async def query_positions(self) -> list[BrokerPosition]:
        out: list[BrokerPosition] = []
        for pos in self._ib.positions():
            contract = getattr(pos, "contract", None)
            symbol = (
                getattr(contract, "symbol", "")
                if contract is not None else ""
            )
            qty = int(getattr(pos, "position", 0) or 0)
            avg_cost = float(getattr(pos, "avgCost", 0.0) or 0.0)
            out.append(
                BrokerPosition(
                    instrument=symbol, quantity=qty, avg_cost=avg_cost,
                )
            )
        return out

    async def query_account(self) -> BrokerAccountState:
        # ib_async accountSummary returns a list of AccountValue rows;
        # we extract the fields the FSM cares about.
        rows = self._ib.accountSummary()
        as_dict: dict[str, float] = {}
        for row in rows:
            tag = getattr(row, "tag", "")
            value = getattr(row, "value", "0.0")
            try:
                as_dict[tag] = float(value)
            except (ValueError, TypeError):
                continue
        return BrokerAccountState(
            available_funds=as_dict.get("AvailableFunds", 0.0),
            net_liquidation=as_dict.get("NetLiquidation", 0.0),
            initial_margin=as_dict.get("InitMarginReq", 0.0),
            maintenance_margin=as_dict.get("MaintMarginReq", 0.0),
            realized_pnl_today=as_dict.get("RealizedPnL"),
            open_pnl=as_dict.get("UnrealizedPnL"),
        )

    def subscribe_transitions(self, handler: TransitionHandler) -> None:
        self._handlers.append(handler)

    def health(self) -> dict[str, Any]:
        return {
            "connected": self._ib.isConnected(),
            "tracked_orders": len(self._orders),
            "active_handlers": len(self._handlers),
        }

    def get_order(self, client_order_id: str) -> ManagedOrder | None:
        return self._orders.get(client_order_id)

    # ── helpers ───────────────────────────────────────────────────

    def _get_contract(self, instrument: str) -> Any:
        if instrument not in self._contract_cache:
            self._contract_cache[instrument] = self._resolve_contract(instrument)
        return self._contract_cache[instrument]

    def _build_ib_order(self, intent: Order, client_order_id: str) -> Any:
        """Build an ib_async order matching the intent. Stamped with
        `orderRef = client_order_id` so the event handler can look up
        the ManagedOrder.
        """
        # Lazy import: keeping ib_async types out of unit tests that
        # use a MockIB.
        from ib_async import LimitOrder, MarketOrder, StopOrder

        action = "BUY" if intent.side == Side.LONG else "SELL"
        if intent.type == OrderType.MARKET:
            order = MarketOrder(action, intent.quantity)
        elif intent.type == OrderType.STOP:
            if intent.stop_price is None:
                raise ValueError("STOP order requires stop_price")
            order = StopOrder(action, intent.quantity, intent.stop_price)
        elif intent.type == OrderType.LIMIT:
            if intent.limit_price is None:
                raise ValueError("LIMIT order requires limit_price")
            order = LimitOrder(action, intent.quantity, intent.limit_price)
        else:
            raise ValueError(f"unsupported OrderType: {intent.type}")
        order.orderRef = client_order_id
        order.tif = "GTC"  # session-end flatten owns end-of-day cleanup
        return order

    def _wire_trade_events(self, client_order_id: str, trade: Any) -> None:
        """Subscribe to the underlying Trade's status + fill events.

        ib_async's Trade exposes `statusEvent` and `fillEvent` from
        eventkit. Both are callable on `+=` to add a handler. We add
        a closure that translates events into apply_transition calls.
        """
        def on_status(t: Any) -> None:
            self._handle_status_event(client_order_id, t)

        def on_fill(t: Any, fill: Any) -> None:
            self._handle_fill_event(client_order_id, t, fill)

        # Defensive: `+=` on absent attributes raises AttributeError; the
        # MockIB used in tests provides these as plain lists.
        if hasattr(trade, "statusEvent"):
            trade.statusEvent += on_status
        if hasattr(trade, "fillEvent"):
            trade.fillEvent += on_fill

    def _handle_status_event(self, client_order_id: str, trade: Any) -> None:
        managed = self._orders.get(client_order_id)
        if managed is None:
            _log.warning(
                "status event for unknown client_order_id %r — ignoring",
                client_order_id,
            )
            return
        if managed.is_terminal:
            return  # extra event after a terminal transition; ignore.

        status = trade.orderStatus
        order = trade.order
        filled = int(getattr(status, "filled", 0) or 0)
        total = int(getattr(order, "totalQuantity", managed.intent.quantity) or 0)
        mapping = map_ibkr_status(status.status, filled=filled, total=total)
        if mapping.target_state is None:
            return  # PendingCancel-style hold; wait for resolution.
        if mapping.target_state == managed.state and mapping.target_state != OrderState.PARTIAL:
            return  # idempotent re-emission; nothing to do.
        try:
            new_state = apply_transition(
                managed, mapping.target_state,
                source=TransitionSource.BROKER,
                reason=f"ibkr_status:{status.status}",
                filled_quantity=filled,
                broker_order_id=str(getattr(order, "orderId", "")) or None,
                detail={"ibkr_status": status.status, "note": mapping.note},
            )
        except Exception as exc:
            _log.error(
                "apply_transition failed for %s: %r — escalating to UNKNOWN",
                client_order_id, exc,
            )
            new_state = apply_transition(
                managed, OrderState.UNKNOWN,
                source=TransitionSource.BROKER,
                reason=f"escalation:{exc!r}",
                detail={"ibkr_status": status.status},
            )
        self._orders[client_order_id] = new_state
        self._notify(new_state, new_state.transitions[-1])

    def _handle_fill_event(
        self, client_order_id: str, trade: Any, fill: Any
    ) -> None:
        # Fill events also fire statusEvent in ib_async; the status
        # handler already applies the transition. We log here for the
        # broker_order_id audit trail; no FSM mutation.
        _log.info(
            "fill: %s qty=%s price=%s",
            client_order_id,
            getattr(getattr(fill, "execution", None), "shares", "?"),
            getattr(getattr(fill, "execution", None), "price", "?"),
        )

    def _notify(
        self, managed: ManagedOrder, last_record
    ) -> None:
        for handler in self._handlers:
            try:
                handler(managed, last_record)
            except Exception as exc:  # noqa: BLE001
                _log.error("transition handler raised: %r", exc)
