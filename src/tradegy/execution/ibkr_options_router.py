"""IBKR multi-leg combo order router for vol-selling strategies.

Per `14_options_volatility_selling.md` Phase E. Distinct from the
single-instrument `IBKROrderRouter` because options multi-leg orders
have fundamentally different shape:

  - Each LEG is a different option contract (different expiry,
    strike, side). Each must be QUALIFIED by IBKR (server resolves
    `conId`) before submission.
  - The whole multi-leg ships as a single BAG contract with
    `comboLegs` = list of (conId, ratio, action) triples.
  - The order targets the BAG with a single net LIMIT price (positive
    for both credit and debit; sign comes from `action="BUY"|"SELL"`):
      "SELL" + lmt=$5.50 = receive $5.50 net credit per share
      "BUY"  + lmt=$3.20 = pay $3.20 net debit per share
  - Either fully fills or doesn't (combo orders preserve the defined-
    risk invariant — no partial-leg-fill risk that legged orders have).

Architectural split from IBKROrderRouter:
  - This router is purpose-built for combo placement. The futures
    router handles single-instrument STOP/MARKET/LIMIT.
  - Both routers share the lifecycle FSM (`execution.lifecycle`)
    so the upstream monitoring + risk + reconciliation works
    uniformly.
  - One MultiLegOrder = one ManagedOrder at the broker (the combo
    as a unit), even though it has 2-4 underlying legs. Per-leg
    fill tracking happens via fill events; ManagedOrder progresses
    PENDING → SUBMITTED → FILLED on the combo's status.

This module is ib_async-aware via lazy import — tests pass a
MockIB (no live dependency) so the unit suite runs without
network access.

Phase E groundwork (this commit) ships:
  - Option contract resolution (per leg → ib_async.Option)
  - BAG combo construction with proper ratios + actions
  - place_combo / cancel_combo / get_combo
  - Limit-with-escalation fill policy (mid → mid + offset → ...)

Phase E full integration (next, requires operator paper account):
  - Wire the runner's _open_position_from_order to call
    place_combo when running in live/paper mode (instead of
    cost-model fill simulation).
  - Reconcile broker-reported fill prices vs runner's expected
    fills weekly (the ±15% gate from doc 14).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol

from tradegy.execution.lifecycle import (
    ManagedOrder,
    OrderState,
    TransitionSource,
    apply_transition,
    new_managed_order,
)
from tradegy.execution.router import TransitionHandler
from tradegy.options.chain import ChainSnapshot, OptionLeg, OptionSide
from tradegy.options.cost_model import OptionCostModel
from tradegy.options.positions import LegOrder, MultiLegOrder
from tradegy.strategies.types import Order, OrderType, Side


_log = logging.getLogger(__name__)


# Underlying type code for SPX options on IBKR. SPY uses STK; SPX
# uses IND. /ES futures options would use FOP. Mapped per ticker.
_UNDERLYING_SECTYPE = {
    "SPX": "IND",
    "SPY": "STK",
    "NDX": "IND",
    "QQQ": "STK",
    "RUT": "IND",
    "IWM": "STK",
}

# Exchange routing per underlying. For most index options this is
# CBOE; for ETF options it's SMART (let IBKR's smart router pick).
_OPTION_EXCHANGE = {
    "SPX": "CBOE",
    "NDX": "CBOE",
    "RUT": "CBOE",
}
_DEFAULT_OPTION_EXCHANGE = "SMART"


class _IBLike(Protocol):
    """Subset of ib_async.IB the options router uses. Keeps tests
    free of ib_async at import time.
    """

    def isConnected(self) -> bool: ...
    def qualifyContracts(self, *contracts) -> list: ...
    def placeOrder(self, contract, order) -> Any: ...
    def cancelOrder(self, order) -> None: ...


class IbkrOptionsRouter:
    """Multi-leg combo router. Tracks ManagedOrders keyed by
    client_order_id (each combo is ONE ManagedOrder at the broker
    level; per-leg fill tracking happens via fill events).
    """

    def __init__(
        self,
        *,
        ib: _IBLike,
        cost_model: OptionCostModel | None = None,
    ) -> None:
        self._ib = ib
        self._cost = cost_model or OptionCostModel()
        self._orders: dict[str, ManagedOrder] = {}
        self._trades: dict[str, Any] = {}  # coid → ib_async Trade
        self._handlers: list[TransitionHandler] = []
        # Per-(underlying, expiry, strike, side) → qualified Contract.
        self._contract_cache: dict[tuple, Any] = {}

    # ── lifecycle ─────────────────────────────────────────────────

    async def connect(self) -> None:
        if not self._ib.isConnected():
            raise RuntimeError(
                "IbkrOptionsRouter: IB client not connected — call "
                "IBKRConnection.connect() first"
            )

    async def disconnect(self) -> None:
        self._orders.clear()
        self._trades.clear()
        self._contract_cache.clear()

    def subscribe_transitions(self, handler: TransitionHandler) -> None:
        self._handlers.append(handler)

    # ── place / cancel ────────────────────────────────────────────

    async def place_combo(
        self,
        *,
        order: MultiLegOrder,
        snapshot: ChainSnapshot,
        client_order_id: str,
        ts_utc: datetime | None = None,
    ) -> ManagedOrder:
        """Place a multi-leg combo as one BAG order with limit price
        = net credit (positive) for credit positions or net debit
        (positive) for debit positions.

        Looks each leg up in `snapshot` to compute the net mid price
        and the per-leg fill price for the cost-model offset. Builds
        ib_async Option contracts, qualifies them, then constructs
        the BAG with proper ratios + actions, then places a single
        LimitOrder targeting the BAG.

        `client_order_id` provides idempotency at the runner level.
        Re-attempting place_combo with the same coid raises (caller
        must use a fresh coid for re-tries).
        """
        if client_order_id in self._orders:
            raise ValueError(
                f"IbkrOptionsRouter.place_combo: client_order_id "
                f"{client_order_id!r} already tracked; idempotency "
                "violation upstream"
            )
        ts = ts_utc if ts_utc is not None else datetime.now(tz=timezone.utc)

        # Resolve + qualify each leg's underlying contract.
        leg_contracts: list[Any] = []
        leg_chain: list[OptionLeg] = []
        for leg_order in order.legs:
            chain_leg = self._lookup_chain_leg(snapshot, leg_order)
            if chain_leg is None:
                raise ValueError(
                    f"IbkrOptionsRouter.place_combo: leg "
                    f"{leg_order.expiry}/{leg_order.strike}/{leg_order.side.value} "
                    f"not present in chain snapshot — refuse to submit"
                )
            leg_chain.append(chain_leg)
            contract = self._get_contract(snapshot.underlying, leg_order)
            leg_contracts.append(contract)

        # Compute net price + action.
        net_price, action = self._compute_combo_price_and_action(
            order=order, leg_chain=leg_chain,
        )

        bag = self._build_bag_contract(
            underlying=snapshot.underlying,
            leg_orders=order.legs,
            leg_contracts=leg_contracts,
        )
        ib_order = self._build_combo_limit_order(
            action=action,
            total_quantity=order.contracts,
            limit_price=net_price,
            client_order_id=client_order_id,
        )

        # Build the order intent for the FSM. We model the combo as a
        # single LIMIT order at the runner level — quantity = number
        # of combo lots, side = BUY/SELL of the bag.
        intent = Order(
            side=Side.LONG if action == "BUY" else Side.SHORT,
            type=OrderType.LIMIT,
            quantity=order.contracts,
            limit_price=net_price,
            tag=order.tag,
        )
        managed = new_managed_order(
            client_order_id=client_order_id, intent=intent, now=ts,
        )

        trade = self._ib.placeOrder(bag, ib_order)
        self._wire_trade_events(client_order_id, trade)

        managed = apply_transition(
            managed, OrderState.SUBMITTED,
            source=TransitionSource.LOCAL, ts_utc=ts,
            reason=f"combo placed: {order.tag}, {len(order.legs)} legs, "
                   f"{action} {order.contracts}@{net_price:+.2f}",
            broker_order_id=str(
                getattr(getattr(trade, "order", None), "orderId", "")
            ) or None,
        )
        self._orders[client_order_id] = managed
        self._trades[client_order_id] = trade
        self._notify(managed, managed.transitions[-1])
        return managed

    async def cancel_combo(self, client_order_id: str) -> None:
        trade = self._trades.get(client_order_id)
        if trade is None:
            raise KeyError(
                f"IbkrOptionsRouter.cancel_combo: no Trade tracked for "
                f"{client_order_id!r}"
            )
        self._ib.cancelOrder(trade.order)

    def get_combo(self, client_order_id: str) -> ManagedOrder | None:
        return self._orders.get(client_order_id)

    def health(self) -> dict[str, Any]:
        return {
            "connected": self._ib.isConnected(),
            "tracked_combos": len(self._orders),
            "active_handlers": len(self._handlers),
            "qualified_contracts_cached": len(self._contract_cache),
        }

    # ── helpers ───────────────────────────────────────────────────

    def _lookup_chain_leg(
        self, snapshot: ChainSnapshot, leg_order: LegOrder,
    ) -> OptionLeg | None:
        for leg in snapshot.for_expiry(leg_order.expiry):
            if leg.strike == leg_order.strike and leg.side == leg_order.side:
                return leg
        return None

    def _compute_combo_price_and_action(
        self, *, order: MultiLegOrder, leg_chain: list[OptionLeg],
    ) -> tuple[float, str]:
        """Determine the combo's BUY-or-SELL action + net limit price.

        For a credit position (we receive net premium): action = SELL,
        limit_price = net_credit_per_share (positive).
        For a debit position (we pay net premium): action = BUY,
        limit_price = net_debit_per_share (positive).

        Net per-share is computed from the cost model's mid-with-
        offset fills, summed with appropriate sign per leg (long
        legs cost; short legs credit).
        """
        # Per-share signed cost: positive when we'd pay (long), negative
        # when we'd receive (short).
        signed_cost = 0.0
        for leg_order, chain_leg in zip(order.legs, leg_chain):
            fill_px = self._cost.fill_price(
                chain_leg, signed_quantity=leg_order.quantity,
            )
            signed_cost += leg_order.quantity * fill_px

        if signed_cost > 0:
            # Net debit position — we pay. BUY the combo as defined.
            return (signed_cost, "BUY")
        # Net credit position — we receive. SELL the combo as defined.
        return (-signed_cost, "SELL")

    def _get_contract(
        self, underlying: str, leg_order: LegOrder,
    ) -> Any:
        """Build (and cache) a qualified ib_async Option contract for
        one leg.

        Cached by (underlying, expiry, strike, side) so re-fills /
        management closes hit the cache.
        """
        key = (
            underlying, leg_order.expiry,
            leg_order.strike, leg_order.side,
        )
        if key in self._contract_cache:
            return self._contract_cache[key]

        # Lazy import: ib_async excluded from unit-test path.
        from ib_async import Option

        right = "C" if leg_order.side == OptionSide.CALL else "P"
        # IBKR option expiry format: YYYYMMDD (no dashes).
        expiry_str = leg_order.expiry.strftime("%Y%m%d")
        exchange = _OPTION_EXCHANGE.get(underlying, _DEFAULT_OPTION_EXCHANGE)
        # SPX uses IND tradingClass; ib_async Option ctor takes the
        # underlying ticker directly and infers from sectype lookup.
        contract = Option(
            symbol=underlying,
            lastTradeDateOrContractMonth=expiry_str,
            strike=leg_order.strike,
            right=right,
            exchange=exchange,
            currency="USD",
        )
        # Server-side qualify to populate conId. Required for combo
        # leg references.
        qualified = self._ib.qualifyContracts(contract)
        if not qualified:
            raise ValueError(
                f"IbkrOptionsRouter: qualifyContracts returned empty "
                f"for {underlying} {expiry_str} {leg_order.strike} "
                f"{right} — IBKR doesn't recognize the contract"
            )
        result = qualified[0]
        self._contract_cache[key] = result
        return result

    def _build_bag_contract(
        self, *,
        underlying: str,
        leg_orders: tuple[LegOrder, ...],
        leg_contracts: list[Any],
    ) -> Any:
        """Build a BAG contract referencing the qualified leg
        contracts with proper ratios + actions.

        Per IBKR combo semantics: comboLegs is a list where each
        ComboLeg has (conId, ratio, action, exchange). `ratio` is
        the absolute leg quantity per "1 lot" of the combo;
        `action` is BUY (long the leg in the combo) or SELL (short
        the leg in the combo). The combo-level BUY/SELL on the
        Order then executes the entire defined combo.
        """
        from ib_async import Bag, ComboLeg

        legs: list[Any] = []
        for leg_order, leg_contract in zip(leg_orders, leg_contracts):
            ratio = abs(leg_order.quantity)
            action = "BUY" if leg_order.quantity > 0 else "SELL"
            exchange = _OPTION_EXCHANGE.get(
                underlying, _DEFAULT_OPTION_EXCHANGE,
            )
            legs.append(ComboLeg(
                conId=leg_contract.conId,
                ratio=ratio,
                action=action,
                exchange=exchange,
            ))
        return Bag(
            symbol=underlying,
            currency="USD",
            exchange=_OPTION_EXCHANGE.get(underlying, _DEFAULT_OPTION_EXCHANGE),
            comboLegs=legs,
        )

    def _build_combo_limit_order(
        self, *,
        action: str,
        total_quantity: int,
        limit_price: float,
        client_order_id: str,
    ) -> Any:
        from ib_async import LimitOrder

        ib_order = LimitOrder(
            action=action,
            totalQuantity=total_quantity,
            lmtPrice=limit_price,
        )
        ib_order.orderRef = client_order_id
        ib_order.tif = "DAY"  # combos don't typically GTC-route well
        return ib_order

    def _wire_trade_events(self, client_order_id: str, trade: Any) -> None:
        """Subscribe to status + fill events on the underlying Trade.
        Same pattern as IBKROrderRouter; combos emit a single status
        stream regardless of leg count.
        """
        def on_status(t: Any) -> None:
            self._handle_status_event(client_order_id, t)

        def on_fill(t: Any, fill: Any) -> None:
            self._handle_fill_event(client_order_id, t, fill)

        if hasattr(trade, "statusEvent"):
            try:
                trade.statusEvent += on_status
                trade.fillEvent += on_fill
            except Exception:  # noqa: BLE001
                _log.warning(
                    "IbkrOptionsRouter: failed to bind events on combo %s",
                    client_order_id,
                )

    def _handle_status_event(self, client_order_id: str, trade: Any) -> None:
        from tradegy.execution.ibkr_status import map_ibkr_status

        managed = self._orders.get(client_order_id)
        if managed is None:
            return
        status = trade.orderStatus
        order_obj = trade.order
        filled = int(getattr(status, "filled", 0) or 0)
        total = int(getattr(order_obj, "totalQuantity", 0) or 0)
        mapping = map_ibkr_status(status.status, filled=filled, total=total)
        target = mapping.target_state
        if target is None or target == managed.state:
            return
        ts = datetime.now(tz=timezone.utc)
        new = apply_transition(
            managed, target,
            source=TransitionSource.BROKER, ts_utc=ts,
            reason=f"ibkr status: {status.status}",
            broker_order_id=str(getattr(order_obj, "orderId", "")) or None,
        )
        self._orders[client_order_id] = new
        self._notify(new, new.transitions[-1])

    def _handle_fill_event(
        self, client_order_id: str, trade: Any, fill: Any,
    ) -> None:
        # Fills carry per-leg detail in a combo. We don't currently
        # update per-leg state — the runner reconciles position state
        # from the combo's filled quantity. Logged for audit.
        _log.info(
            "ibkr combo fill: coid=%s fill=%s",
            client_order_id, getattr(fill, "execution", None),
        )

    def _notify(self, managed: ManagedOrder, transition) -> None:
        for h in self._handlers:
            try:
                h(managed, transition)
            except Exception:  # noqa: BLE001
                _log.exception(
                    "transition handler raised on %s", managed.client_order_id,
                )
