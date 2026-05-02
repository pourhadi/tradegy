"""HealthCheckRunner — runs every registered check on each tick.

Per `12_live_monitoring_spec.md`. The runner is the orchestration
boundary: it owns the list of checks to run and the alert router
they feed into. One tick = one pass through every check.

Production wiring puts the runner inside an asyncio loop with a
configurable cadence (default 1s — the finest health-check SLO in
doc 12 § Health-check inventory). The `tick(now)` method is
deterministic and synchronous; the async outer loop is just an
asyncio.sleep wrapper.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from tradegy.monitoring.alerts import AlertRouter
from tradegy.monitoring.check import HealthCheck
from tradegy.monitoring.types import HealthCheckResult, Severity, severity_rank


_log = logging.getLogger(__name__)


@dataclass
class RunReport:
    """Per-tick summary returned by `tick()`. Aggregate snapshot used
    by the live-monitoring dashboard and tests.
    """

    ts_utc: datetime
    results: list[HealthCheckResult] = field(default_factory=list)

    @property
    def worst_severity(self) -> Severity:
        if not self.results:
            return Severity.OK
        return max(self.results, key=lambda r: severity_rank(r.severity)).severity

    @property
    def critical_count(self) -> int:
        return sum(1 for r in self.results if r.severity == Severity.CRITICAL)

    @property
    def warning_count(self) -> int:
        return sum(1 for r in self.results if r.severity == Severity.WARNING)


class HealthCheckRunner:
    """Owns a list of HealthChecks + an AlertRouter; runs them all on
    each `tick(now)`. Exceptions in a check do not cascade — they're
    logged and turned into a CRITICAL result for the offending check.
    """

    def __init__(
        self,
        *,
        checks: list[HealthCheck],
        alert_router: AlertRouter,
    ) -> None:
        # Defensive copy of the checks list so the caller can rebuild
        # without invalidating us mid-tick.
        self._checks = list(checks)
        self._router = alert_router

    def tick(self, *, now: datetime) -> RunReport:
        """Run every check once; route every non-OK result through the
        alert router; return a RunReport with all results.
        """
        report = RunReport(ts_utc=now)
        for check in self._checks:
            try:
                result = check.check(now=now)
            except Exception as exc:  # noqa: BLE001
                _log.error("check %s raised: %r", check.id, exc)
                result = HealthCheckResult(
                    check_id=check.id,
                    severity=Severity.CRITICAL,
                    message=f"check raised: {exc!r}",
                    ts_utc=now,
                    detail={"exception_type": type(exc).__name__},
                )
            report.results.append(result)
            if result.severity != Severity.OK:
                self._router.route(result)
        return report

    def add_check(self, check: HealthCheck) -> None:
        self._checks.append(check)
