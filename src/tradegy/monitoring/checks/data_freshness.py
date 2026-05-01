"""Live-bar data freshness check.

Per doc 12 § Health-check inventory: SLOs vary by bar cadence.

  5s bars: ≤ 7s    → OK
           > 7s    → WARNING
           > 30s   → CRITICAL

  1s bars: ≤ 3s    → OK
           > 3s    → WARNING
           > 10s   → CRITICAL

Source: `LiveAdapter.health()['last_seen']` (a UTC datetime — the
timestamp of the most recent bar yielded by the adapter).

The check accepts a `cadence_label` so a single instance can serve
both 5s and 1s adapters via threshold tables. The default is the 5s
profile.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from tradegy.monitoring.check import HealthCheck
from tradegy.monitoring.types import (
    AlertAction,
    HealthCheckResult,
    Severity,
)


# Default thresholds keyed by cadence label.
_THRESHOLDS: dict[str, tuple[float, float]] = {
    "5s": (7.0, 30.0),
    "1s": (3.0, 10.0),
}


class DataFreshnessCheck(HealthCheck):
    """Verifies the live-feed `last_seen` timestamp is current.

    The injected `snapshot` callable returns a dict with at least
    `last_seen: datetime` (UTC) and optionally `within_session: bool`.
    Outside-session bars (overnight, weekends, holidays) are not a
    breach — the check returns OK with `note=outside_session`.
    """

    id = "data_freshness"

    def __init__(
        self,
        *,
        snapshot: Callable[[], dict[str, Any]],
        cadence_label: str = "5s",
        warning_threshold_s: float | None = None,
        critical_threshold_s: float | None = None,
        check_id_suffix: str | None = None,
    ) -> None:
        self._snapshot = snapshot
        defaults = _THRESHOLDS.get(cadence_label, (7.0, 30.0))
        self._warn = (
            warning_threshold_s if warning_threshold_s is not None else defaults[0]
        )
        self._crit = (
            critical_threshold_s if critical_threshold_s is not None else defaults[1]
        )
        self._cadence_label = cadence_label
        # Per-instance check id so multiple feeds on the same runner
        # have distinct dedup keys.
        self.id = (
            f"data_freshness:{check_id_suffix or cadence_label}"
        )

    def check(self, *, now: datetime) -> HealthCheckResult:
        try:
            snap = self._snapshot()
        except Exception as exc:  # noqa: BLE001
            return HealthCheckResult(
                check_id=self.id,
                severity=Severity.CRITICAL,
                message=f"data-feed snapshot raised: {exc!r}",
                ts_utc=now,
                detail={"exception_type": type(exc).__name__},
                auto_action=AlertAction.FLATTEN_AND_HALT_STRATEGY,
            )

        if not snap.get("within_session", True):
            return HealthCheckResult(
                check_id=self.id,
                severity=Severity.OK,
                message="outside session — freshness check skipped",
                ts_utc=now,
                detail={"cadence": self._cadence_label, "outside_session": True},
            )

        last_seen = snap.get("last_seen")
        if last_seen is None:
            return HealthCheckResult(
                check_id=self.id,
                severity=Severity.WARNING,
                message="data feed has no last_seen yet",
                ts_utc=now,
                detail={"cadence": self._cadence_label},
            )
        if not isinstance(last_seen, datetime):
            return HealthCheckResult(
                check_id=self.id,
                severity=Severity.CRITICAL,
                message=(
                    f"data feed last_seen has wrong type: "
                    f"{type(last_seen).__name__}"
                ),
                ts_utc=now,
                detail={"cadence": self._cadence_label},
                auto_action=AlertAction.FLATTEN_AND_HALT_STRATEGY,
            )

        age_s = (now - last_seen).total_seconds()
        if age_s > self._crit:
            return HealthCheckResult(
                check_id=self.id,
                severity=Severity.CRITICAL,
                message=(
                    f"{self._cadence_label} bar {age_s:.1f}s stale "
                    f"(> {self._crit:.1f}s critical)"
                ),
                ts_utc=now,
                detail={
                    "cadence": self._cadence_label, "age_s": age_s,
                    "last_seen": last_seen.isoformat(),
                },
                auto_action=AlertAction.FLATTEN_AND_HALT_STRATEGY,
            )
        if age_s > self._warn:
            return HealthCheckResult(
                check_id=self.id,
                severity=Severity.WARNING,
                message=(
                    f"{self._cadence_label} bar {age_s:.1f}s stale "
                    f"(> {self._warn:.1f}s warning)"
                ),
                ts_utc=now,
                detail={
                    "cadence": self._cadence_label, "age_s": age_s,
                    "last_seen": last_seen.isoformat(),
                },
            )
        return HealthCheckResult(
            check_id=self.id,
            severity=Severity.OK,
            message=f"{self._cadence_label} bar fresh ({age_s:.1f}s)",
            ts_utc=now,
            detail={"cadence": self._cadence_label, "age_s": age_s},
        )
