"""Tests for the four Phase-1 concrete health checks."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradegy.monitoring.checks import (
    BrokerConnectivityCheck,
    DataFreshnessCheck,
    ProcessLivenessCheck,
    TimeSkewCheck,
)
from tradegy.monitoring.types import AlertAction, Severity


def _ts(seconds: float = 0) -> datetime:
    base = datetime(2026, 5, 1, 14, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=seconds)


# ── BrokerConnectivityCheck ──────────────────────────────────────


def test_broker_connected_with_fresh_heartbeat_is_ok():
    snap = lambda: {"connected": True, "last_heartbeat_age_s": 0.5}
    c = BrokerConnectivityCheck(snapshot=snap)
    r = c.check(now=_ts())
    assert r.severity == Severity.OK


def test_broker_disconnected_is_critical():
    snap = lambda: {"connected": False, "last_heartbeat_age_s": 99.0}
    c = BrokerConnectivityCheck(snapshot=snap)
    r = c.check(now=_ts())
    assert r.severity == Severity.CRITICAL
    assert r.auto_action == AlertAction.GLOBAL_KILL_SWITCH


def test_broker_stale_heartbeat_warning_to_critical():
    c = BrokerConnectivityCheck(
        snapshot=lambda: {"connected": True, "last_heartbeat_age_s": 3.0},
        warning_threshold_s=2.0, critical_threshold_s=5.0,
    )
    assert c.check(now=_ts()).severity == Severity.WARNING

    c2 = BrokerConnectivityCheck(
        snapshot=lambda: {"connected": True, "last_heartbeat_age_s": 7.0},
        warning_threshold_s=2.0, critical_threshold_s=5.0,
    )
    r2 = c2.check(now=_ts())
    assert r2.severity == Severity.CRITICAL
    assert r2.auto_action == AlertAction.GLOBAL_KILL_SWITCH


def test_broker_snapshot_exception_is_critical():
    def raises():
        raise RuntimeError("network down")

    c = BrokerConnectivityCheck(snapshot=raises)
    r = c.check(now=_ts())
    assert r.severity == Severity.CRITICAL
    assert "network down" in r.message


def test_broker_unknown_heartbeat_is_ok_when_connected():
    """Older snapshot shape without heartbeat age — graceful degrade."""
    c = BrokerConnectivityCheck(
        snapshot=lambda: {"connected": True},
    )
    assert c.check(now=_ts()).severity == Severity.OK


# ── DataFreshnessCheck ──────────────────────────────────────────


def test_data_fresh_under_threshold_is_ok():
    last = _ts(-3)  # 3s ago
    c = DataFreshnessCheck(
        snapshot=lambda: {"last_seen": last, "within_session": True},
        cadence_label="5s",
    )
    assert c.check(now=_ts()).severity == Severity.OK


def test_data_stale_5s_warning():
    last = _ts(-15)  # 15s ago, > 7s warning, < 30s critical
    c = DataFreshnessCheck(
        snapshot=lambda: {"last_seen": last, "within_session": True},
        cadence_label="5s",
    )
    r = c.check(now=_ts())
    assert r.severity == Severity.WARNING


def test_data_stale_5s_critical():
    last = _ts(-60)
    c = DataFreshnessCheck(
        snapshot=lambda: {"last_seen": last, "within_session": True},
        cadence_label="5s",
    )
    r = c.check(now=_ts())
    assert r.severity == Severity.CRITICAL
    assert r.auto_action == AlertAction.FLATTEN_AND_HALT_STRATEGY


def test_data_1s_thresholds_tighter():
    last = _ts(-5)  # 5s ago — 1s cadence makes this CRITICAL.
    c = DataFreshnessCheck(
        snapshot=lambda: {"last_seen": last, "within_session": True},
        cadence_label="1s",
    )
    assert c.check(now=_ts()).severity == Severity.WARNING

    c_crit = DataFreshnessCheck(
        snapshot=lambda: {"last_seen": _ts(-15), "within_session": True},
        cadence_label="1s",
    )
    assert c_crit.check(now=_ts()).severity == Severity.CRITICAL


def test_data_outside_session_skips_check():
    last = _ts(-3600)  # an hour ago, but outside RTH.
    c = DataFreshnessCheck(
        snapshot=lambda: {"last_seen": last, "within_session": False},
        cadence_label="5s",
    )
    r = c.check(now=_ts())
    assert r.severity == Severity.OK
    assert r.detail["outside_session"] is True


def test_data_no_last_seen_is_warning():
    c = DataFreshnessCheck(
        snapshot=lambda: {"last_seen": None, "within_session": True},
        cadence_label="5s",
    )
    assert c.check(now=_ts()).severity == Severity.WARNING


def test_data_check_id_uses_suffix_for_dedup():
    c5 = DataFreshnessCheck(
        snapshot=lambda: {"last_seen": _ts(), "within_session": True},
        cadence_label="5s",
    )
    c1 = DataFreshnessCheck(
        snapshot=lambda: {"last_seen": _ts(), "within_session": True},
        cadence_label="1s",
    )
    assert c5.id != c1.id


# ── TimeSkewCheck ───────────────────────────────────────────────


def test_skew_within_tolerance_is_ok():
    c = TimeSkewCheck(broker_now=lambda: _ts(-0.3))
    assert c.check(now=_ts()).severity == Severity.OK


def test_skew_warning():
    c = TimeSkewCheck(broker_now=lambda: _ts(-1.5))
    assert c.check(now=_ts()).severity == Severity.WARNING


def test_skew_critical():
    c = TimeSkewCheck(broker_now=lambda: _ts(-3.5))
    r = c.check(now=_ts())
    assert r.severity == Severity.CRITICAL
    assert r.auto_action == AlertAction.GLOBAL_KILL_SWITCH


def test_skew_negative_direction_also_detected():
    """Local clock BEHIND broker (negative delta in our convention)."""
    c = TimeSkewCheck(broker_now=lambda: _ts(+3.5))
    assert c.check(now=_ts()).severity == Severity.CRITICAL


def test_skew_broker_time_unknown_is_warning():
    c = TimeSkewCheck(broker_now=lambda: None)
    assert c.check(now=_ts()).severity == Severity.WARNING


def test_skew_broker_time_raises_is_warning():
    def raises():
        raise RuntimeError("nope")

    c = TimeSkewCheck(broker_now=raises)
    r = c.check(now=_ts())
    assert r.severity == Severity.WARNING


# ── ProcessLivenessCheck ────────────────────────────────────────


def test_process_liveness_no_heartbeat_yet_is_warning():
    c = ProcessLivenessCheck()
    assert c.check(now=_ts()).severity == Severity.WARNING


def test_process_liveness_fresh_heartbeat_is_ok():
    c = ProcessLivenessCheck(critical_threshold_s=5.0)
    c.heartbeat(now=_ts(-1))
    assert c.check(now=_ts()).severity == Severity.OK


def test_process_liveness_stale_heartbeat_is_critical():
    c = ProcessLivenessCheck(critical_threshold_s=5.0)
    c.heartbeat(now=_ts(-10))
    r = c.check(now=_ts())
    assert r.severity == Severity.CRITICAL
    assert r.auto_action == AlertAction.GLOBAL_KILL_SWITCH


def test_process_liveness_records_age_in_detail():
    c = ProcessLivenessCheck()
    c.heartbeat(now=_ts(-2))
    r = c.check(now=_ts())
    assert "age_s" in r.detail
    assert abs(r.detail["age_s"] - 2.0) < 0.01
