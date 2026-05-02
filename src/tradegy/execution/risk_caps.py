"""Hard risk-cap enforcement for the execution layer.

Per `11_execution_layer_spec.md:260-279`. The risk-cap module owns the
pre-order gate: every new order intent passes through `pre_flight_check`
before the FSM transitions out of PENDING. A failed check produces
`REJECTED` with a structured reason code; the order never leaves the
local boundary.

Caps come from two sources:

  - **Per-strategy** caps from `RiskEnvelopeSpec` in each strategy spec
    (`max_concurrent_instances`, `max_daily_loss_pct`,
    `max_weekly_loss_pct` per `04_strategy_spec_schema.md` operational
    section).
  - **Operator-level** caps applied across all strategies (e.g., total
    max-concurrent positions, total daily loss).

The check order matches doc 11 §263-273 exactly. Any check can be
disabled by passing `None` for that field; the production wiring
populates every field from the operational config.

This module is broker-agnostic and synchronous. Real-time margin /
heartbeat checks (steps 1, 5 in the doc) require live-system inputs
and are wired in Phase 3 — they're declared in `RiskCaps` but evaluated
only when their input fields are populated.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum

from tradegy.execution.lifecycle import OrderState
from tradegy.strategies.types import Order


class CheckResult(str, Enum):
    PASS = "pass"
    FAIL = "fail"


class RejectReason(str, Enum):
    """Structured reason codes for pre-flight rejection. The set mirrors
    the order-of-evaluation in doc 11 §263-273.
    """

    BROKER_DISCONNECTED = "broker_disconnected"
    OUTSIDE_TRADING_HOURS = "outside_trading_hours"
    DAILY_LOSS_CAP_BREACH = "daily_loss_cap_breach"
    WEEKLY_LOSS_CAP_BREACH = "weekly_loss_cap_breach"
    MAX_CONCURRENT_POSITIONS = "max_concurrent_positions"
    MAX_CONCURRENT_INSTANCES = "max_concurrent_instances"
    INSUFFICIENT_MARGIN = "insufficient_margin"
    STRATEGY_DISABLED = "strategy_disabled"
    PROPOSAL_ONLY_TIER = "proposal_only_tier"
    KILL_SWITCH_ACTIVE = "kill_switch_active"


@dataclass(frozen=True)
class RiskCheckResult:
    result: CheckResult
    reason: RejectReason | None = None
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.result == CheckResult.PASS


@dataclass
class RiskState:
    """Live counters that the pre-flight gate inspects.

    Updated by the execution layer in two places:
      - on fill events (positions open / close)
      - on session boundaries (daily/weekly counters reset per spec rules)

    Attributes are deliberately concrete numbers — the gate evaluates a
    snapshot, not a stream. Keep this dataclass small; complex
    derivations belong in the caller.
    """

    realized_pnl_today: float = 0.0
    open_pnl: float = 0.0
    realized_pnl_this_week: float = 0.0
    open_position_count: int = 0  # across the whole account
    open_instances_per_strategy: dict[str, int] = field(default_factory=dict)
    broker_connected: bool = True
    broker_last_heartbeat_age_s: float = 0.0
    within_trading_hours: bool = True
    available_margin: float = float("inf")  # broker-reported
    estimated_margin_per_contract: float = 0.0  # broker-reported
    auto_disabled_strategies: frozenset[str] = frozenset()


@dataclass(frozen=True)
class RiskCaps:
    """Aggregated caps applied at pre-flight. None disables that check."""

    daily_loss_cap_dollars: float | None = None
    weekly_loss_cap_dollars: float | None = None
    max_concurrent_positions_total: int | None = None
    max_concurrent_instances_per_strategy: int | None = None
    heartbeat_max_age_s: float = 2.0  # doc 11 §264
    kill_switch_active: bool = False
    enforce_proposal_only_tier_block: bool = True


def pre_flight_check(
    *,
    order: Order,
    strategy_id: str,
    strategy_tier: str,  # "auto_execute" | "confirm_then_execute" | "proposal_only"
    state: RiskState,
    caps: RiskCaps,
) -> RiskCheckResult:
    """Evaluate the doc 11 §263-273 checklist in order. Short-circuits
    on first failure. Returns a structured RiskCheckResult.

    The check order is load-bearing: cheaper / harder-failure checks
    run first so the rejection reason names the most useful blocker.
    """
    # 0. Kill-switch — checked before anything else; if active, no
    # order can pass regardless of other state.
    if caps.kill_switch_active:
        return RiskCheckResult(
            CheckResult.FAIL, RejectReason.KILL_SWITCH_ACTIVE,
            "kill-switch active — all new orders blocked",
        )

    # 1. Connection healthy.
    if not state.broker_connected:
        return RiskCheckResult(
            CheckResult.FAIL, RejectReason.BROKER_DISCONNECTED,
            "broker reports disconnected",
        )
    if state.broker_last_heartbeat_age_s > caps.heartbeat_max_age_s:
        return RiskCheckResult(
            CheckResult.FAIL, RejectReason.BROKER_DISCONNECTED,
            (
                f"last broker heartbeat {state.broker_last_heartbeat_age_s:.1f}s "
                f"old (max {caps.heartbeat_max_age_s}s)"
            ),
        )

    # 2. Within trading hours.
    if not state.within_trading_hours:
        return RiskCheckResult(
            CheckResult.FAIL, RejectReason.OUTSIDE_TRADING_HOURS,
            "instrument session not active",
        )

    # 3. Daily loss cap.
    if caps.daily_loss_cap_dollars is not None:
        floor = -abs(caps.daily_loss_cap_dollars)
        if state.realized_pnl_today + state.open_pnl <= floor:
            return RiskCheckResult(
                CheckResult.FAIL, RejectReason.DAILY_LOSS_CAP_BREACH,
                (
                    f"realized+open PnL "
                    f"{state.realized_pnl_today + state.open_pnl:+.2f} ≤ "
                    f"-{abs(caps.daily_loss_cap_dollars):.2f}"
                ),
            )
    if caps.weekly_loss_cap_dollars is not None:
        floor = -abs(caps.weekly_loss_cap_dollars)
        if state.realized_pnl_this_week + state.open_pnl <= floor:
            return RiskCheckResult(
                CheckResult.FAIL, RejectReason.WEEKLY_LOSS_CAP_BREACH,
                f"weekly P&L {state.realized_pnl_this_week:+.2f} ≤ {floor:+.2f}",
            )

    # 4a. Concurrent position cap (account-level).
    if caps.max_concurrent_positions_total is not None:
        if state.open_position_count >= caps.max_concurrent_positions_total:
            return RiskCheckResult(
                CheckResult.FAIL, RejectReason.MAX_CONCURRENT_POSITIONS,
                (
                    f"open position count {state.open_position_count} "
                    f"≥ cap {caps.max_concurrent_positions_total}"
                ),
            )

    # 4b. Concurrent instances per strategy.
    if caps.max_concurrent_instances_per_strategy is not None:
        n = state.open_instances_per_strategy.get(strategy_id, 0)
        if n >= caps.max_concurrent_instances_per_strategy:
            return RiskCheckResult(
                CheckResult.FAIL, RejectReason.MAX_CONCURRENT_INSTANCES,
                (
                    f"strategy {strategy_id} has {n} open instances "
                    f"≥ cap {caps.max_concurrent_instances_per_strategy}"
                ),
            )

    # 5. Margin sufficiency.
    if state.estimated_margin_per_contract > 0:
        needed = state.estimated_margin_per_contract * order.quantity
        if needed > state.available_margin:
            return RiskCheckResult(
                CheckResult.FAIL, RejectReason.INSUFFICIENT_MARGIN,
                (
                    f"need {needed:.2f} margin for {order.quantity} contract(s); "
                    f"available {state.available_margin:.2f}"
                ),
            )

    # 6. Strategy-level enabled / tier check.
    if strategy_id in state.auto_disabled_strategies:
        return RiskCheckResult(
            CheckResult.FAIL, RejectReason.STRATEGY_DISABLED,
            f"strategy {strategy_id} is auto-disabled",
        )
    if caps.enforce_proposal_only_tier_block and strategy_tier == "proposal_only":
        return RiskCheckResult(
            CheckResult.FAIL, RejectReason.PROPOSAL_ONLY_TIER,
            f"strategy {strategy_id} is tier=proposal_only",
        )

    return RiskCheckResult(CheckResult.PASS)


def update_on_fill(
    state: RiskState,
    *,
    strategy_id: str,
    pnl_realized_delta: float = 0.0,
    open_pnl_delta: float = 0.0,
    position_delta: int = 0,
) -> RiskState:
    """Apply a fill event to the running risk state.

    `position_delta` is +1 when a flat-to-open fill lands, -1 when a
    close fill flattens, 0 for a partial that doesn't change open count.
    The caller decides; this function does NOT infer from the order.
    """
    new_per_strategy = dict(state.open_instances_per_strategy)
    if position_delta != 0:
        cur = new_per_strategy.get(strategy_id, 0)
        new_per_strategy[strategy_id] = max(0, cur + position_delta)

    return replace(
        state,
        realized_pnl_today=state.realized_pnl_today + pnl_realized_delta,
        realized_pnl_this_week=state.realized_pnl_this_week + pnl_realized_delta,
        open_pnl=state.open_pnl + open_pnl_delta,
        open_position_count=max(0, state.open_position_count + position_delta),
        open_instances_per_strategy=new_per_strategy,
    )


def reset_daily_counters(state: RiskState) -> RiskState:
    """Called at the start of each session: zero realized_today + open_pnl
    snapshot. Weekly counter persists.
    """
    return replace(
        state,
        realized_pnl_today=0.0,
        open_pnl=0.0,
    )


def reset_weekly_counters(state: RiskState) -> RiskState:
    """Called at the start of each trading week."""
    return replace(state, realized_pnl_this_week=0.0)
