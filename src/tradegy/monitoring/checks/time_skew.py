"""Time-skew check vs broker.

Per doc 12 § Health-check inventory: target SLO is ≤ 2s skew between
local UTC clock and the broker-reported timestamp; CRITICAL on
breach. Per § Auto-halt triggers, time skew > 2s vs broker is
listed under the CRITICAL examples (§108).

Skew can be in either direction (local ahead OR behind). The check
takes absolute value; the detail records the signed delta so
operators can diagnose which side drifted.

Source: a callable returning the broker's reported `now` timestamp.
In production, this is read from a periodic IBKR account-summary or
quote message that carries server time. For testing, an injected
callable returns a controlled value.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from tradegy.monitoring.check import HealthCheck
from tradegy.monitoring.types import (
    AlertAction,
    HealthCheckResult,
    Severity,
)


class TimeSkewCheck(HealthCheck):
    """Compares local UTC `now` with broker-reported `now`.

    `broker_now` callable returns the broker's view of current time
    (UTC). If the callable returns None or raises, the check emits
    WARNING (broker time unknown) — NOT CRITICAL — because a missing
    server time isn't itself a trade-blocking condition.
    """

    id = "time_skew"

    def __init__(
        self,
        *,
        broker_now: Callable[[], datetime | None],
        warning_threshold_s: float = 1.0,
        critical_threshold_s: float = 2.0,
    ) -> None:
        self._broker_now = broker_now
        self._warn = warning_threshold_s
        self._crit = critical_threshold_s

    def check(self, *, now: datetime) -> HealthCheckResult:
        try:
            broker_ts = self._broker_now()
        except Exception as exc:  # noqa: BLE001
            return HealthCheckResult(
                check_id=self.id,
                severity=Severity.WARNING,
                message=f"broker time read raised: {exc!r}",
                ts_utc=now,
                detail={"exception_type": type(exc).__name__},
            )
        if broker_ts is None:
            return HealthCheckResult(
                check_id=self.id,
                severity=Severity.WARNING,
                message="broker time unknown",
                ts_utc=now,
            )
        delta_s = (now - broker_ts).total_seconds()
        abs_s = abs(delta_s)
        if abs_s > self._crit:
            return HealthCheckResult(
                check_id=self.id,
                severity=Severity.CRITICAL,
                message=(
                    f"time skew {delta_s:+.2f}s "
                    f"exceeds critical threshold {self._crit:.1f}s"
                ),
                ts_utc=now,
                detail={
                    "delta_s": delta_s,
                    "broker_ts": broker_ts.isoformat(),
                    "local_ts": now.isoformat(),
                },
                auto_action=AlertAction.GLOBAL_KILL_SWITCH,
            )
        if abs_s > self._warn:
            return HealthCheckResult(
                check_id=self.id,
                severity=Severity.WARNING,
                message=(
                    f"time skew {delta_s:+.2f}s exceeds warning threshold "
                    f"{self._warn:.1f}s"
                ),
                ts_utc=now,
                detail={"delta_s": delta_s},
            )
        return HealthCheckResult(
            check_id=self.id,
            severity=Severity.OK,
            message=f"time skew {delta_s:+.2f}s within tolerance",
            ts_utc=now,
            detail={"delta_s": delta_s},
        )
