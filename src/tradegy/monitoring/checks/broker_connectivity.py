"""Broker-connectivity check.

Per doc 12 § Health-check inventory: target SLO is connected with
heartbeat ≤ 2s. Source: `IBKRConnection.health()` at
`live/ibkr.py:64-73` (the same dict that the live adapter exposes).

Severity ladder:
  - connected, heartbeat ≤ 2s          → OK
  - connected, heartbeat in (2s, 5s]   → WARNING
  - connected, heartbeat > 5s          → CRITICAL
  - disconnected                       → CRITICAL  (per §65 + auto-halt §146:
                                                    broker disconnect ≥ 30s
                                                    triggers global kill)
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from tradegy.monitoring.check import HealthCheck
from tradegy.monitoring.types import (
    AlertAction,
    HealthCheckResult,
    Severity,
)


class BrokerConnectivityCheck(HealthCheck):
    """Probes broker connectivity via an injected health snapshot
    callable.

    The callable returns a dict in the shape produced by
    `IBKRConnection.health()`: at minimum
        {"connected": bool, "last_heartbeat_age_s": float, ...}

    `last_heartbeat_age_s` may be absent in older snapshot shapes; the
    check treats absence as "unknown — fall back to connected/not".
    """

    id = "broker_connectivity"

    def __init__(
        self,
        *,
        snapshot: Callable[[], dict[str, Any]],
        warning_threshold_s: float = 2.0,
        critical_threshold_s: float = 5.0,
    ) -> None:
        self._snapshot = snapshot
        self._warn = warning_threshold_s
        self._crit = critical_threshold_s

    def check(self, *, now: datetime) -> HealthCheckResult:
        try:
            snap = self._snapshot()
        except Exception as exc:  # noqa: BLE001
            return HealthCheckResult(
                check_id=self.id,
                severity=Severity.CRITICAL,
                message=f"broker health snapshot raised: {exc!r}",
                ts_utc=now,
                detail={"exception_type": type(exc).__name__},
                auto_action=AlertAction.GLOBAL_KILL_SWITCH,
            )

        connected = bool(snap.get("connected"))
        if not connected:
            return HealthCheckResult(
                check_id=self.id,
                severity=Severity.CRITICAL,
                message="broker disconnected",
                ts_utc=now,
                detail=dict(snap),
                auto_action=AlertAction.GLOBAL_KILL_SWITCH,
            )

        age = snap.get("last_heartbeat_age_s")
        if age is None:
            return HealthCheckResult(
                check_id=self.id,
                severity=Severity.OK,
                message="broker connected (heartbeat age unknown)",
                ts_utc=now,
                detail=dict(snap),
            )
        age_f = float(age)
        if age_f > self._crit:
            return HealthCheckResult(
                check_id=self.id,
                severity=Severity.CRITICAL,
                message=(
                    f"broker heartbeat {age_f:.1f}s old (> {self._crit:.1f}s)"
                ),
                ts_utc=now,
                detail=dict(snap),
                auto_action=AlertAction.GLOBAL_KILL_SWITCH,
            )
        if age_f > self._warn:
            return HealthCheckResult(
                check_id=self.id,
                severity=Severity.WARNING,
                message=(
                    f"broker heartbeat {age_f:.1f}s old (> {self._warn:.1f}s)"
                ),
                ts_utc=now,
                detail=dict(snap),
            )
        return HealthCheckResult(
            check_id=self.id,
            severity=Severity.OK,
            message=f"broker connected (heartbeat {age_f:.1f}s)",
            ts_utc=now,
            detail=dict(snap),
        )
