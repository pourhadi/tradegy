"""Kill-switch tests.

Covers the trip / clear / mark_reconciled lifecycle and the restart
contract from doc 11 §306-313.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tradegy.execution.kill_switch import (
    KillSwitch,
    KillSwitchState,
    TripSource,
)


def _ts(seconds: int = 0) -> datetime:
    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    return base.replace(second=seconds % 60, minute=base.minute + seconds // 60)


def test_starts_inactive():
    ks = KillSwitch()
    assert ks.state == KillSwitchState.INACTIVE
    assert not ks.is_active()
    assert ks.last_trip is None


def test_trip_moves_to_active():
    ks = KillSwitch().trip(
        source=TripSource.DAILY_LOSS_CAP,
        reason="-500 reached",
        ts_utc=_ts(),
    )
    assert ks.state == KillSwitchState.ACTIVE
    assert ks.is_active()
    assert ks.last_trip is not None
    assert ks.last_trip.source == TripSource.DAILY_LOSS_CAP


def test_trip_is_idempotent_on_active():
    ks = KillSwitch().trip(source=TripSource.OPERATOR, reason="manual")
    ks2 = ks.trip(source=TripSource.MARGIN_CALL, reason="margin call")
    # Still ACTIVE; new record appended.
    assert ks2.state == KillSwitchState.ACTIVE
    assert len(ks2.trip_history) == 2
    # last_trip returns the most recent trip.
    assert ks2.last_trip.source == TripSource.MARGIN_CALL


def test_clear_requires_operator():
    ks = KillSwitch().trip(source=TripSource.OPERATOR, reason="x")
    with pytest.raises(ValueError):
        ks.clear(operator="", reason="resolved")


def test_clear_moves_to_awaiting_reconciliation():
    ks = (
        KillSwitch()
        .trip(source=TripSource.DAILY_LOSS_CAP, reason="x")
        .clear(operator="dan", reason="root cause analyzed", ts_utc=_ts(60))
    )
    # Per restart contract: not yet INACTIVE.
    assert ks.state == KillSwitchState.AWAITING_RECONCILIATION
    assert ks.is_active()  # orders still blocked


def test_clear_on_inactive_raises():
    with pytest.raises(ValueError):
        KillSwitch().clear(operator="dan", reason="x")


def test_mark_reconciled_completes_restart():
    ks = (
        KillSwitch()
        .trip(source=TripSource.MARGIN_CALL, reason="x")
        .clear(operator="dan", reason="resolved")
        .mark_reconciled(operator="dan")
    )
    assert ks.state == KillSwitchState.INACTIVE
    assert not ks.is_active()


def test_mark_reconciled_requires_correct_state():
    # Cannot mark_reconciled directly from ACTIVE; must clear first.
    ks = KillSwitch().trip(source=TripSource.OPERATOR, reason="x")
    with pytest.raises(ValueError):
        ks.mark_reconciled(operator="dan")


def test_audit_history_is_complete():
    ks = (
        KillSwitch()
        .trip(source=TripSource.DAILY_LOSS_CAP, reason="loss")
        .clear(operator="dan", reason="resolved")
        .mark_reconciled(operator="dan", detail={"open_orders": 0})
    )
    events = [r.event for r in ks.trip_history]
    assert events == ["trip", "clear", "reconciled"]


def test_re_trip_after_full_cycle_works():
    ks = (
        KillSwitch()
        .trip(source=TripSource.DAILY_LOSS_CAP, reason="x")
        .clear(operator="dan", reason="resolved")
        .mark_reconciled(operator="dan")
    )
    assert ks.state == KillSwitchState.INACTIVE
    ks2 = ks.trip(source=TripSource.OPERATOR, reason="emergency")
    assert ks2.state == KillSwitchState.ACTIVE
    assert ks2.is_active()


def test_immutable_replace_returns_new_instance():
    ks = KillSwitch()
    ks2 = ks.trip(source=TripSource.OPERATOR, reason="x")
    # Original unchanged.
    assert ks.state == KillSwitchState.INACTIVE
    assert ks2.state == KillSwitchState.ACTIVE
    assert ks.trip_history == ()
    assert len(ks2.trip_history) == 1
