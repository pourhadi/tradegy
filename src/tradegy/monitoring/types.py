"""Monitoring layer enums + result dataclasses.

Per `12_live_monitoring_spec.md` § Alert severity matrix and § Auto-
halt triggers. The types here are the canonical wire format every
HealthCheck returns and every AlertHandler consumes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """Per doc 12 § Alert severity matrix.

    OK is implicit (the check passed) but is represented explicitly so
    health endpoints can distinguish "ran and passed" from "didn't run".
    """

    OK = "ok"
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# Severity rank for comparison operations. Higher = more severe.
_RANK: dict[Severity, int] = {
    Severity.OK: 0,
    Severity.INFO: 1,
    Severity.WARNING: 2,
    Severity.CRITICAL: 3,
}


def severity_rank(s: Severity) -> int:
    return _RANK[s]


class AlertAction(str, Enum):
    """Per doc 12 § Auto-halt triggers (§126-148). Three canonical
    auto-halt actions in increasing severity, plus NONE for
    informational results.
    """

    NONE = "none"
    NO_NEW_ENTRY = "no_new_entry"
    """Reject new entry orders for the affected strategy; protective
    stops and exits continue normally."""

    FLATTEN_AND_HALT_STRATEGY = "flatten_and_halt_strategy"
    """Move the affected strategy to auto_disabled; flatten its open
    positions via MARKET with ExitReason.OVERRIDE."""

    GLOBAL_KILL_SWITCH = "global_kill_switch"
    """Whole-system halt per `11_execution_layer_spec.md` global
    kill-switch contract."""


@dataclass(frozen=True)
class HealthCheckResult:
    """One health check's verdict.

    `dedup_key` is the stable identifier the alert router uses for
    deduplication — same key fired within the cooldown window is
    suppressed. Defaults to `f"{check_id}:{severity}"` if not set.
    """

    check_id: str
    severity: Severity
    message: str
    ts_utc: datetime
    detail: dict[str, Any] = field(default_factory=dict)
    auto_action: AlertAction = AlertAction.NONE
    affected_strategy_id: str | None = None
    dedup_key: str | None = None

    @property
    def passed(self) -> bool:
        return self.severity == Severity.OK

    @property
    def effective_dedup_key(self) -> str:
        return self.dedup_key or f"{self.check_id}:{self.severity.value}"
