"""Local-vs-broker divergence detection.

Per `11_execution_layer_spec.md:227-237`. The reconciliation loop
periodically calls the router's `query_open_orders`, `query_positions`,
`query_account` methods and feeds the result into this detector. The
detector is a pure function: same inputs → same outputs, no clock,
no broker, fully testable.

Per spec, **the broker is the source of truth for positions and
fills.** Local state catches up to broker reality, never the other way
(the only exception is the kill-switch which forcibly flattens). The
detector encodes this discipline: every divergence event names a
recommended action that mutates *local* state to match broker, never
the reverse.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from tradegy.execution.lifecycle import ManagedOrder, OrderState
from tradegy.execution.router import (
    BrokerAccountState,
    BrokerOrderState,
    BrokerPosition,
)


class DivergenceType(str, Enum):
    """One per row of doc 11 §227-237."""

    ORDER_MISSING_AT_BROKER = "order_missing_at_broker"
    """Local sees order WORKING; broker has no record. Resolution:
    move to UNKNOWN, retry query, mark REJECTED after 5s if still
    absent."""

    LOCAL_FILLED_BROKER_WORKING = "local_filled_broker_working"
    """Local sees FILLED; broker still shows WORKING. Trust broker:
    reopen local + investigate fill source."""

    POSITION_LOCAL_BROKER_FLAT = "position_local_broker_flat"
    """Local has a position; broker shows flat. Trust broker: flatten
    local, emit CRITICAL alert."""

    POSITION_LOCAL_FLAT_BROKER_OPEN = "position_local_flat_broker_open"
    """Local is flat; broker has a position. Trust broker: submit
    market order to flatten broker side, emit CRITICAL alert."""

    POSITION_QUANTITY_MISMATCH = "position_quantity_mismatch"
    """Both sides have a position in the same instrument but signed
    quantity differs. Trust broker: reconcile."""

    MARGIN_BREACH = "margin_breach"
    """Account-level: maintenance margin exceeds available funds.
    Block new orders, trigger session-end flatten, CRITICAL."""


class DivergenceSeverity(str, Enum):
    HIGH = "high"
    CRITICAL = "critical"


class RecommendedAction(str, Enum):
    """The detector recommends; the orchestration layer (Phase 3C)
    dispatches. Actions are stable strings the loop maps to actual
    operations.
    """

    MARK_LOCAL_UNKNOWN = "mark_local_unknown"
    REOPEN_LOCAL_ORDER = "reopen_local_order"
    FLATTEN_LOCAL = "flatten_local"
    FLATTEN_BROKER = "flatten_broker"
    BLOCK_NEW_ORDERS_AND_FLATTEN = "block_new_orders_and_flatten"
    INVESTIGATE = "investigate"


@dataclass(frozen=True)
class DivergenceEvent:
    type: DivergenceType
    severity: DivergenceSeverity
    action: RecommendedAction
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


def detect_order_divergences(
    *,
    local_orders: dict[str, ManagedOrder],
    broker_orders: list[BrokerOrderState],
) -> list[DivergenceEvent]:
    """Compare local-tracked orders against the broker's open-order
    list. Two divergence types:

      * Local non-terminal but missing at broker → ORDER_MISSING_AT_BROKER.
      * Local FILLED but broker still WORKING → LOCAL_FILLED_BROKER_WORKING.
    """
    out: list[DivergenceEvent] = []
    by_coid = {b.client_order_id: b for b in broker_orders}

    for coid, mo in local_orders.items():
        if coid not in by_coid:
            # Order not present at broker. Two cases:
            #   - Local already terminal — no divergence (broker discards
            #     terminal orders from open-orders list).
            #   - Local still active — divergence.
            if not mo.is_terminal:
                out.append(
                    DivergenceEvent(
                        type=DivergenceType.ORDER_MISSING_AT_BROKER,
                        severity=DivergenceSeverity.HIGH,
                        action=RecommendedAction.MARK_LOCAL_UNKNOWN,
                        message=(
                            f"local order {coid} is {mo.state.value} but "
                            "broker has no record"
                        ),
                        detail={"client_order_id": coid, "local_state": mo.state.value},
                    )
                )
            continue
        bo = by_coid[coid]
        # Local FILLED while broker still WORKING is a real divergence
        # (broker is source of truth — local raced ahead somehow).
        if mo.state == OrderState.FILLED and bo.state in (
            OrderState.WORKING, OrderState.PARTIAL, OrderState.SUBMITTED,
        ):
            out.append(
                DivergenceEvent(
                    type=DivergenceType.LOCAL_FILLED_BROKER_WORKING,
                    severity=DivergenceSeverity.CRITICAL,
                    action=RecommendedAction.REOPEN_LOCAL_ORDER,
                    message=(
                        f"local order {coid} marked FILLED but broker "
                        f"shows {bo.state.value}"
                    ),
                    detail={
                        "client_order_id": coid,
                        "local_state": mo.state.value,
                        "broker_state": bo.state.value,
                    },
                )
            )
    return out


def detect_position_divergences(
    *,
    local_positions: dict[str, int],
    broker_positions: list[BrokerPosition],
) -> list[DivergenceEvent]:
    """Compare local-tracked positions (by instrument → signed qty)
    against the broker's reported positions. Three divergence types:

      * Local has position, broker is flat (zero or absent)
      * Local is flat (zero or absent), broker has position
      * Both have positions but signed quantities differ
    """
    out: list[DivergenceEvent] = []
    bro_by_instrument = {p.instrument: p for p in broker_positions}
    instruments = set(local_positions) | set(bro_by_instrument)

    for inst in instruments:
        local_q = int(local_positions.get(inst, 0))
        broker_q = int(
            bro_by_instrument[inst].quantity if inst in bro_by_instrument else 0
        )
        if local_q == broker_q:
            continue
        if local_q != 0 and broker_q == 0:
            out.append(
                DivergenceEvent(
                    type=DivergenceType.POSITION_LOCAL_BROKER_FLAT,
                    severity=DivergenceSeverity.CRITICAL,
                    action=RecommendedAction.FLATTEN_LOCAL,
                    message=(
                        f"local position {inst}={local_q:+d} but broker "
                        "is flat"
                    ),
                    detail={
                        "instrument": inst,
                        "local_quantity": local_q,
                        "broker_quantity": broker_q,
                    },
                )
            )
        elif local_q == 0 and broker_q != 0:
            out.append(
                DivergenceEvent(
                    type=DivergenceType.POSITION_LOCAL_FLAT_BROKER_OPEN,
                    severity=DivergenceSeverity.CRITICAL,
                    action=RecommendedAction.FLATTEN_BROKER,
                    message=(
                        f"local is flat for {inst} but broker shows "
                        f"{broker_q:+d}"
                    ),
                    detail={
                        "instrument": inst,
                        "local_quantity": local_q,
                        "broker_quantity": broker_q,
                    },
                )
            )
        else:
            out.append(
                DivergenceEvent(
                    type=DivergenceType.POSITION_QUANTITY_MISMATCH,
                    severity=DivergenceSeverity.CRITICAL,
                    action=RecommendedAction.FLATTEN_BROKER,
                    message=(
                        f"position mismatch for {inst}: local "
                        f"{local_q:+d} vs broker {broker_q:+d}"
                    ),
                    detail={
                        "instrument": inst,
                        "local_quantity": local_q,
                        "broker_quantity": broker_q,
                    },
                )
            )
    return out


def detect_account_divergences(
    *,
    broker_account: BrokerAccountState,
) -> list[DivergenceEvent]:
    """Account-level checks. Currently: maintenance margin breach
    (maintenance_margin exceeds available_funds). Per doc 11 §233 this
    is CRITICAL — block new orders, trigger session-end flatten.
    """
    out: list[DivergenceEvent] = []
    if (
        broker_account.maintenance_margin > 0
        and broker_account.maintenance_margin > broker_account.available_funds
    ):
        out.append(
            DivergenceEvent(
                type=DivergenceType.MARGIN_BREACH,
                severity=DivergenceSeverity.CRITICAL,
                action=RecommendedAction.BLOCK_NEW_ORDERS_AND_FLATTEN,
                message=(
                    f"maintenance margin {broker_account.maintenance_margin:.2f} "
                    f"exceeds available funds "
                    f"{broker_account.available_funds:.2f}"
                ),
                detail={
                    "maintenance_margin": broker_account.maintenance_margin,
                    "available_funds": broker_account.available_funds,
                    "net_liquidation": broker_account.net_liquidation,
                },
            )
        )
    return out


def detect_all_divergences(
    *,
    local_orders: dict[str, ManagedOrder],
    broker_orders: list[BrokerOrderState],
    local_positions: dict[str, int],
    broker_positions: list[BrokerPosition],
    broker_account: BrokerAccountState | None = None,
) -> list[DivergenceEvent]:
    """Run all detectors in spec order: orders, then positions, then
    account. Returns a flat list; orchestration layer dispatches by
    type/severity.
    """
    out = list(detect_order_divergences(
        local_orders=local_orders, broker_orders=broker_orders,
    ))
    out.extend(detect_position_divergences(
        local_positions=local_positions, broker_positions=broker_positions,
    ))
    if broker_account is not None:
        out.extend(detect_account_divergences(broker_account=broker_account))
    return out
