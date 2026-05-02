"""Broker-agnostic order router interface.

Per `11_execution_layer_spec.md` Phase 3. The router sits between the
strategy intent stream (Order objects emitted by `on_bar`) and the
broker. Every broker integration (IBKR for v1) implements this
interface.

Order lifecycle responsibility split:
  - Strategy class emits `Order` intents.
  - Risk-cap pre-flight (`risk_caps.pre_flight_check`) gates submission.
  - Router builds the idempotency key, calls broker `place`, returns
    a `ManagedOrder` in PENDING / SUBMITTED.
  - Broker emits status events; router translates each into
    `apply_transition(...)` and notifies subscribers via the event hook.
  - Reconciliation loop (Phase 3B) polls `query_open_orders` /
    `query_positions` / `query_account` and detects divergence.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tradegy.execution.lifecycle import ManagedOrder, OrderState, TransitionRecord


@dataclass(frozen=True)
class BrokerOrderState:
    """Snapshot of one broker-side order. Used by the reconciliation
    loop to detect divergence with the local FSM.
    """

    client_order_id: str
    broker_order_id: str
    state: OrderState
    filled_quantity: int
    remaining_quantity: int
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerPosition:
    instrument: str
    quantity: int  # signed: + long, - short, 0 flat
    avg_cost: float
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerAccountState:
    """Subset of broker account state the execution layer cares about.
    Margin & equity are required; the rest is best-effort and may be
    None for brokers that don't expose it.
    """

    available_funds: float
    net_liquidation: float
    initial_margin: float
    maintenance_margin: float
    realized_pnl_today: float | None = None
    open_pnl: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# Subscriber signature for transition notifications. The router calls
# the handler whenever an order changes state. Handlers should be cheap
# (don't block the event loop); long-running work belongs in a queue.
TransitionHandler = Callable[[ManagedOrder, TransitionRecord], None]


class BrokerRouter(ABC):
    """Abstract broker order router.

    Lifecycle:
        connect() → place(intent) → ... events drive apply_transition ... → disconnect()

    Implementations are async-friendly; place/cancel/query methods are
    coroutines so callers can `await` them inside an event loop.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Establish broker connectivity. Idempotent."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Tear down broker connectivity. Idempotent."""

    @abstractmethod
    async def place(
        self,
        *,
        intent,  # tradegy.strategies.types.Order
        client_order_id: str,
        instrument: str,
        ts_utc: datetime | None = None,
    ) -> ManagedOrder:
        """Submit an order intent under the given idempotency key.

        Returns the freshly-built ManagedOrder (in SUBMITTED or earlier
        if the broker has already responded synchronously). Subsequent
        state changes arrive via `subscribe_transitions`.
        """

    @abstractmethod
    async def cancel(self, client_order_id: str) -> None:
        """Cancel an order by client_order_id. The actual transition to
        CANCELLED arrives via the event stream — this method only sends
        the cancel request.
        """

    @abstractmethod
    async def query_open_orders(self) -> list[BrokerOrderState]:
        """Fetch the broker's view of currently-open orders. Used by
        the reconciliation loop (Phase 3B).
        """

    @abstractmethod
    async def query_positions(self) -> list[BrokerPosition]:
        """Fetch broker-reported positions for every instrument."""

    @abstractmethod
    async def query_account(self) -> BrokerAccountState:
        """Fetch broker account state (margin, equity, daily PnL)."""

    @abstractmethod
    def subscribe_transitions(self, handler: TransitionHandler) -> None:
        """Register a handler invoked on every state transition the
        router applies. Multiple handlers may be registered; they run
        in registration order.
        """

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Connection health snapshot for live monitoring."""

    @abstractmethod
    def get_order(self, client_order_id: str) -> ManagedOrder | None:
        """Return the current local view of an order, or None if not
        tracked.
        """
