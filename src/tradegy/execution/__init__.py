"""Execution layer — order lifecycle FSM, idempotency, transition log.

Per `11_execution_layer_spec.md`. This is the foundation that the live
IBKR adapter, broker reconciliation loop, and kill-switch sit on top of.

Phase 1 (this module) ships the deterministic, broker-agnostic core:
the state machine, the idempotency-key generator, and the append-only
transition log. The live adapter integration (Phase 2) and the risk-
cap / kill-switch enforcement (Phase 3) layer on top of these.
"""
from tradegy.execution.idempotency import (
    IdempotencyKeyDeduper,
    OrderRole,
    make_client_order_id,
    parse_client_order_id,
)
from tradegy.execution.lifecycle import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    IllegalTransition,
    ManagedOrder,
    OrderState,
    TransitionRecord,
    TransitionSource,
    apply_transition,
)
from tradegy.execution.log import TransitionLog

__all__ = [
    "IdempotencyKeyDeduper",
    "IllegalTransition",
    "LEGAL_TRANSITIONS",
    "ManagedOrder",
    "OrderRole",
    "OrderState",
    "TERMINAL_STATES",
    "TransitionLog",
    "TransitionRecord",
    "TransitionSource",
    "apply_transition",
    "make_client_order_id",
    "parse_client_order_id",
]
