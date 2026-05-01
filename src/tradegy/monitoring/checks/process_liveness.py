"""Process-liveness watchdog.

Per doc 12 § Health-check inventory: target SLO is heartbeat ≤ 5s
from the runtime watchdog; CRITICAL on breach. This check sits at
the bottom of the monitoring stack — if the process itself stops
heartbeating, every other check is suspect.

The watchdog is a simple counter: production code calls
`heartbeat()` from the main loop on every iteration; the check
asserts the timestamp is recent enough.

For testing, the watchdog is constructed with an injected `now`
provider so tests can advance time.
"""
from __future__ import annotations

from datetime import datetime, timezone

from tradegy.monitoring.check import HealthCheck
from tradegy.monitoring.types import (
    AlertAction,
    HealthCheckResult,
    Severity,
)


class ProcessLivenessCheck(HealthCheck):
    """Tracks the most-recent `heartbeat()` timestamp and reports
    CRITICAL if the gap exceeds the threshold.

    A fresh instance has `last_heartbeat = None`; the check returns
    WARNING in that state (process hasn't reported yet) rather than
    CRITICAL, so startup transients don't trip the kill-switch
    immediately.
    """

    id = "process_liveness"

    def __init__(
        self,
        *,
        critical_threshold_s: float = 5.0,
    ) -> None:
        self._crit = critical_threshold_s
        self._last_heartbeat: datetime | None = None

    def heartbeat(self, *, now: datetime | None = None) -> None:
        """Record a heartbeat. Production code calls this from the main
        loop. `now` defaults to UTC now() but can be overridden by
        tests.
        """
        self._last_heartbeat = (
            now if now is not None else datetime.now(tz=timezone.utc)
        )

    def check(self, *, now: datetime) -> HealthCheckResult:
        if self._last_heartbeat is None:
            return HealthCheckResult(
                check_id=self.id,
                severity=Severity.WARNING,
                message="process has not heartbeat yet",
                ts_utc=now,
            )
        age_s = (now - self._last_heartbeat).total_seconds()
        if age_s > self._crit:
            return HealthCheckResult(
                check_id=self.id,
                severity=Severity.CRITICAL,
                message=(
                    f"process heartbeat {age_s:.1f}s old "
                    f"(> {self._crit:.1f}s critical)"
                ),
                ts_utc=now,
                detail={
                    "age_s": age_s,
                    "last_heartbeat": self._last_heartbeat.isoformat(),
                },
                auto_action=AlertAction.GLOBAL_KILL_SWITCH,
            )
        return HealthCheckResult(
            check_id=self.id,
            severity=Severity.OK,
            message=f"process alive ({age_s:.1f}s since last heartbeat)",
            ts_utc=now,
            detail={"age_s": age_s},
        )
