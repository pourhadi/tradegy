"""Live monitoring layer.

Per `12_live_monitoring_spec.md`. Phase 1 ships the health-check
framework (HealthCheck ABC, runner, alert router) and four
deterministic concrete checks: broker connectivity, data freshness,
time skew vs broker, process liveness. Phase 2 layers on the
upstream-system-dependent checks (feature drift, model freshness,
LLM availability, selection-layer cycle health).

The runner is broker- and source-agnostic: every concrete check is a
small `HealthCheck` subclass that consumes whatever inputs it needs
(injected at construction). The runner has no opinion about IBKR or
any specific feature — it just runs registered checks and routes
results through the alert router.
"""
from tradegy.monitoring.alerts import AlertRouter, AlertHandler
from tradegy.monitoring.check import HealthCheck, register_check
from tradegy.monitoring.runner import HealthCheckRunner, RunReport
from tradegy.monitoring.types import (
    AlertAction,
    HealthCheckResult,
    Severity,
)

__all__ = [
    "AlertAction",
    "AlertHandler",
    "AlertRouter",
    "HealthCheck",
    "HealthCheckResult",
    "HealthCheckRunner",
    "RunReport",
    "Severity",
    "register_check",
]
