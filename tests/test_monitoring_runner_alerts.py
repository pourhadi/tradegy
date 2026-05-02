"""Tests for HealthCheckRunner + AlertRouter."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradegy.monitoring.alerts import AlertRouter, DEFAULT_COOLDOWNS
from tradegy.monitoring.check import HealthCheck
from tradegy.monitoring.runner import HealthCheckRunner
from tradegy.monitoring.types import (
    AlertAction,
    HealthCheckResult,
    Severity,
)


def _ts(seconds: float = 0) -> datetime:
    base = datetime(2026, 5, 1, 14, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=seconds)


# ── A tiny test HealthCheck ─────────────────────────────────────


class _StaticCheck(HealthCheck):
    def __init__(self, *, id: str, severity: Severity, message: str = "x"):
        self.id = id
        self._sev = severity
        self._msg = message

    def check(self, *, now: datetime) -> HealthCheckResult:
        return HealthCheckResult(
            check_id=self.id, severity=self._sev, message=self._msg, ts_utc=now,
        )


class _RaisingCheck(HealthCheck):
    id = "raises"

    def check(self, *, now: datetime) -> HealthCheckResult:
        raise RuntimeError("kaboom")


# ── HealthCheckRunner ──────────────────────────────────────────


def test_runner_returns_all_results():
    r = HealthCheckRunner(
        checks=[
            _StaticCheck(id="a", severity=Severity.OK),
            _StaticCheck(id="b", severity=Severity.WARNING),
            _StaticCheck(id="c", severity=Severity.CRITICAL),
        ],
        alert_router=AlertRouter(),
    )
    rep = r.tick(now=_ts())
    assert len(rep.results) == 3
    assert rep.warning_count == 1
    assert rep.critical_count == 1
    assert rep.worst_severity == Severity.CRITICAL


def test_runner_routes_only_non_ok_results():
    seen = []
    router = AlertRouter()
    router.add_handler(Severity.WARNING, lambda r: seen.append(("warn", r)))
    router.add_handler(Severity.CRITICAL, lambda r: seen.append(("crit", r)))

    r = HealthCheckRunner(
        checks=[
            _StaticCheck(id="ok", severity=Severity.OK),
            _StaticCheck(id="warn", severity=Severity.WARNING),
            _StaticCheck(id="crit", severity=Severity.CRITICAL),
        ],
        alert_router=router,
    )
    r.tick(now=_ts())
    routed = {tag: rec.check_id for tag, rec in seen}
    assert routed == {"warn": "warn", "crit": "crit"}


def test_runner_converts_check_exceptions_to_critical():
    seen_alerts = []
    router = AlertRouter()
    router.add_handler(Severity.CRITICAL, lambda r: seen_alerts.append(r))

    r = HealthCheckRunner(checks=[_RaisingCheck()], alert_router=router)
    rep = r.tick(now=_ts())
    [result] = rep.results
    assert result.severity == Severity.CRITICAL
    assert "kaboom" in result.message
    assert seen_alerts and seen_alerts[0] is result


def test_runner_add_check_extends_list():
    router = AlertRouter()
    r = HealthCheckRunner(checks=[], alert_router=router)
    assert r.tick(now=_ts()).results == []
    r.add_check(_StaticCheck(id="late", severity=Severity.WARNING))
    rep = r.tick(now=_ts())
    assert len(rep.results) == 1


def test_runner_worst_severity_for_empty_is_ok():
    rep = HealthCheckRunner(checks=[], alert_router=AlertRouter()).tick(now=_ts())
    assert rep.worst_severity == Severity.OK


# ── AlertRouter ────────────────────────────────────────────────


def test_router_dispatches_to_severity_handler():
    seen = []
    router = AlertRouter()
    router.add_handler(Severity.WARNING, lambda r: seen.append(r))
    rec = HealthCheckResult(
        check_id="x", severity=Severity.WARNING, message="m", ts_utc=_ts(),
    )
    fired = router.route(rec)
    assert fired
    assert len(seen) == 1


def test_router_skips_ok_results():
    seen = []
    router = AlertRouter()
    router.add_handler(Severity.WARNING, lambda r: seen.append(r))
    rec = HealthCheckResult(
        check_id="x", severity=Severity.OK, message="m", ts_utc=_ts(),
    )
    fired = router.route(rec)
    assert not fired
    assert seen == []


def test_router_dedup_within_window():
    seen = []
    router = AlertRouter(cooldowns={Severity.WARNING: 60.0})
    router.add_handler(Severity.WARNING, lambda r: seen.append(r))

    base = _ts(0)
    rec = lambda t: HealthCheckResult(
        check_id="x", severity=Severity.WARNING, message="m", ts_utc=t,
    )
    assert router.route(rec(base)) is True
    # 30s later — within 60s cooldown — suppressed.
    assert router.route(rec(_ts(30))) is False
    assert len(seen) == 1


def test_router_re_fires_after_cooldown():
    seen = []
    router = AlertRouter(cooldowns={Severity.WARNING: 30.0})
    router.add_handler(Severity.WARNING, lambda r: seen.append(r))
    rec = lambda t: HealthCheckResult(
        check_id="x", severity=Severity.WARNING, message="m", ts_utc=t,
    )
    router.route(rec(_ts(0)))
    assert router.route(rec(_ts(31))) is True
    assert len(seen) == 2


def test_router_distinct_dedup_keys_dont_collide():
    seen = []
    router = AlertRouter(cooldowns={Severity.WARNING: 60.0})
    router.add_handler(Severity.WARNING, lambda r: seen.append(r))
    rec = lambda key: HealthCheckResult(
        check_id="x", severity=Severity.WARNING, message="m",
        ts_utc=_ts(), dedup_key=key,
    )
    router.route(rec("a"))
    router.route(rec("b"))
    assert len(seen) == 2


def test_router_critical_cooldown_5min_default():
    """Per doc 12 §117-118 default CRITICAL re-fire is every 5 min."""
    assert DEFAULT_COOLDOWNS[Severity.CRITICAL] == 5 * 60.0
    assert DEFAULT_COOLDOWNS[Severity.WARNING] == 15 * 60.0


def test_router_handler_exception_does_not_break_other_handlers():
    seen = []
    router = AlertRouter()

    def bad(r):
        raise RuntimeError("x")

    def good(r):
        seen.append(r)

    router.add_handler(Severity.WARNING, bad)
    router.add_handler(Severity.WARNING, good)

    rec = HealthCheckResult(
        check_id="x", severity=Severity.WARNING, message="m", ts_utc=_ts(),
    )
    router.route(rec)
    assert len(seen) == 1


def test_router_no_handlers_for_severity_is_silent_pass():
    router = AlertRouter()
    rec = HealthCheckResult(
        check_id="x", severity=Severity.WARNING, message="m", ts_utc=_ts(),
    )
    assert router.route(rec) is True  # not deduped, just no handlers
