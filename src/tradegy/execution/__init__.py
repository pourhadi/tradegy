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
from tradegy.execution.kill_switch import (
    KillSwitch,
    KillSwitchState,
    TripRecord,
    TripSource,
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
from tradegy.execution.reconciliation import (
    DEFAULT_CADENCES,
    CheckType,
    ReconciliationLoop,
    TickReport,
)
from tradegy.execution.risk_caps import (
    CheckResult,
    RejectReason,
    RiskCaps,
    RiskCheckResult,
    RiskState,
    pre_flight_check,
    reset_daily_counters,
    reset_weekly_counters,
    update_on_fill,
)
from tradegy.execution.divergence import (
    DivergenceEvent,
    DivergenceSeverity,
    DivergenceType,
    RecommendedAction,
    detect_account_divergences,
    detect_all_divergences,
    detect_order_divergences,
    detect_position_divergences,
)
from tradegy.execution.router import (
    BrokerAccountState,
    BrokerOrderState,
    BrokerPosition,
    BrokerRouter,
    TransitionHandler,
)
from tradegy.execution.session_flatten import (
    OpenPosition,
    SessionFlattenPlan,
    build_kill_switch_plan,
    build_session_end_plan,
)

__all__ = [
    "BrokerAccountState",
    "BrokerOrderState",
    "BrokerPosition",
    "BrokerRouter",
    "CheckResult",
    "CheckType",
    "DEFAULT_CADENCES",
    "DivergenceEvent",
    "DivergenceSeverity",
    "DivergenceType",
    "IdempotencyKeyDeduper",
    "IllegalTransition",
    "KillSwitch",
    "KillSwitchState",
    "LEGAL_TRANSITIONS",
    "ManagedOrder",
    "OpenPosition",
    "OrderRole",
    "OrderState",
    "ReconciliationLoop",
    "RecommendedAction",
    "RejectReason",
    "RiskCaps",
    "RiskCheckResult",
    "RiskState",
    "SessionFlattenPlan",
    "TERMINAL_STATES",
    "TickReport",
    "TransitionHandler",
    "TransitionLog",
    "TransitionRecord",
    "TransitionSource",
    "TripRecord",
    "TripSource",
    "apply_transition",
    "build_kill_switch_plan",
    "build_session_end_plan",
    "detect_account_divergences",
    "detect_all_divergences",
    "detect_order_divergences",
    "detect_position_divergences",
    "make_client_order_id",
    "parse_client_order_id",
    "pre_flight_check",
    "reset_daily_counters",
    "reset_weekly_counters",
    "update_on_fill",
]
