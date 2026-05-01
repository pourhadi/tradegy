"""Global kill-switch.

Per `11_execution_layer_spec.md:284-313`. A single boolean controls
execution-layer behavior. When set, the execution layer:

  1. Rejects every new `Order` with reason `kill_switch_active`.
  2. Cancels every `WORKING`/`PARTIAL` order.
  3. Submits `MARKET` flatten orders for every non-flat position with
     `tag = "kill_switch"`. ExitReason recorded as `OVERRIDE`.
  4. Holds the kill state until explicit operator clear.

Trigger authority (doc 11 §296-304):

  - Daily loss cap breach            → automatic
  - Margin call from broker          → automatic
  - Position-vs-broker divergence    → automatic
  - Operator command                 → authenticated CLI
  - Auto-disable cascade (≥3 strats) → automatic

Restart contract (doc 11 §306-313):

  1. Operator confirmation that the underlying issue is understood.
  2. Fresh reconciliation pass (positions, margin, open orders) BEFORE
     accepting any new submissions.
  3. Audit-log entry: who restarted, when, why.

This module enforces the contract; the production wiring connects:
  - the auto-trip triggers (risk caps, divergence detector) → `trip()`
  - the operator CLI → `clear()`
  - every `pre_flight_check` → `is_active()` (already wired in
    `risk_caps.pre_flight_check`)
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum


class TripSource(str, Enum):
    """Who/what tripped the kill-switch. Mirrors the trigger-authority
    table in doc 11 §296-304.
    """

    DAILY_LOSS_CAP = "daily_loss_cap"
    WEEKLY_LOSS_CAP = "weekly_loss_cap"
    MARGIN_CALL = "margin_call"
    BROKER_DIVERGENCE = "broker_divergence"
    OPERATOR = "operator"
    AUTO_DISABLE_CASCADE = "auto_disable_cascade"
    SESSION_FLATTEN_TIMEOUT = "session_flatten_timeout"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TripRecord:
    """One trip / clear event. Append-only; the full sequence is the
    audit history per doc 11 §313.
    """

    ts_utc: datetime
    event: str  # "trip" | "clear"
    source: TripSource
    reason: str
    actor: str = ""  # operator id for "clear"; empty for automatic trips
    detail: dict = field(default_factory=dict)


class KillSwitchState(str, Enum):
    INACTIVE = "inactive"
    ACTIVE = "active"
    AWAITING_RECONCILIATION = "awaiting_reconciliation"


@dataclass
class KillSwitch:
    """Stateful kill-switch with audit trail.

    A KillSwitch is created in INACTIVE. `trip()` moves it to ACTIVE
    and records a TripRecord. `clear(operator, reason)` moves it to
    AWAITING_RECONCILIATION (NOT directly back to INACTIVE — per the
    restart contract, the operator must complete a reconciliation pass
    via `mark_reconciled()` before the switch returns to INACTIVE and
    new orders are accepted).
    """

    state: KillSwitchState = KillSwitchState.INACTIVE
    trip_history: tuple[TripRecord, ...] = ()

    def is_active(self) -> bool:
        """True if new orders should be blocked. Includes the
        AWAITING_RECONCILIATION state — orders stay blocked until the
        operator confirms reconciliation per the restart contract.
        """
        return self.state != KillSwitchState.INACTIVE

    def trip(
        self,
        *,
        source: TripSource,
        reason: str,
        actor: str = "",
        ts_utc: datetime | None = None,
        detail: dict | None = None,
    ) -> "KillSwitch":
        """Trip the switch. Idempotent: tripping an already-active
        switch records the new event but does not change the state
        (the prior trip is already blocking everything).
        """
        ts = ts_utc if ts_utc is not None else datetime.now(tz=timezone.utc)
        rec = TripRecord(
            ts_utc=ts, event="trip", source=source, reason=reason,
            actor=actor, detail=dict(detail or {}),
        )
        return replace(
            self,
            state=KillSwitchState.ACTIVE,
            trip_history=self.trip_history + (rec,),
        )

    def clear(
        self,
        *,
        operator: str,
        reason: str,
        ts_utc: datetime | None = None,
    ) -> "KillSwitch":
        """Operator-only clear. Moves to AWAITING_RECONCILIATION; the
        switch does NOT yet accept new orders. Production wiring then
        runs a fresh reconciliation pass and calls `mark_reconciled()`.
        Calling `clear()` on an already-INACTIVE switch raises.
        """
        if not operator:
            raise ValueError("clear() requires non-empty operator id")
        if self.state == KillSwitchState.INACTIVE:
            raise ValueError("kill-switch is already INACTIVE")
        ts = ts_utc if ts_utc is not None else datetime.now(tz=timezone.utc)
        rec = TripRecord(
            ts_utc=ts, event="clear", source=TripSource.OPERATOR,
            reason=reason, actor=operator,
        )
        return replace(
            self,
            state=KillSwitchState.AWAITING_RECONCILIATION,
            trip_history=self.trip_history + (rec,),
        )

    def mark_reconciled(
        self,
        *,
        operator: str,
        ts_utc: datetime | None = None,
        detail: dict | None = None,
    ) -> "KillSwitch":
        """Final step of the restart contract. Records that a fresh
        reconciliation pass completed; switch returns to INACTIVE and
        new orders are accepted again.
        """
        if self.state != KillSwitchState.AWAITING_RECONCILIATION:
            raise ValueError(
                f"mark_reconciled requires AWAITING_RECONCILIATION state "
                f"(currently {self.state.value})"
            )
        ts = ts_utc if ts_utc is not None else datetime.now(tz=timezone.utc)
        rec = TripRecord(
            ts_utc=ts, event="reconciled",
            source=TripSource.OPERATOR, reason="reconciliation passed",
            actor=operator, detail=dict(detail or {}),
        )
        return replace(
            self,
            state=KillSwitchState.INACTIVE,
            trip_history=self.trip_history + (rec,),
        )

    @property
    def last_trip(self) -> TripRecord | None:
        for rec in reversed(self.trip_history):
            if rec.event == "trip":
                return rec
        return None
