"""Unit tests for the trailing_atr stop adjustment class.

Promoted to Phase 1 of the regime-gated range-scalp plan after the MVP
results showed asymmetric-exit infrastructure is the binding
constraint, not regime selection (commit 6953eef).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import tradegy.strategies.auxiliary_classes  # noqa: F401  — register
from tradegy.strategies.auxiliary import get_stop_adjustment_class
from tradegy.strategies.types import Bar, FeatureSnapshot, Position, Side


def _bar(ts_min: int, high: float, low: float, close: float) -> Bar:
    return Bar(
        ts_utc=datetime(2024, 1, 2, 14, ts_min, tzinfo=timezone.utc),
        open=close,
        high=high,
        low=low,
        close=close,
        volume=100,
    )


def _features(atr: float | None) -> FeatureSnapshot:
    return FeatureSnapshot(
        ts_utc=datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
        values={"mes_atr_14m": atr} if atr is not None else {},
    )


# ── basic long behavior ───────────────────────────────────────────────


def test_trailing_atr_long_no_favorable_move_returns_none():
    """At entry with peak == entry, candidate stop = entry - mult*ATR.
    If that's worse than initial_stop, return None."""
    cls = get_stop_adjustment_class("trailing_atr")
    pos = Position(
        quantity=1,
        avg_entry_price=100.0,
        initial_stop_price=98.0,
        current_stop_price=98.0,
        peak_favorable_price=100.0,  # no favorable move yet
    )
    # ATR = 1.0, mult = 1.5 → trail offset = 1.5
    # Candidate stop = 100 - 1.5 = 98.5; current is 98.0; 98.5 > 98.0
    # so it ratchets up.
    out = cls.adjusted_stop(
        {"atr_feature_id": "mes_atr_14m", "multiplier": 1.5, "tick_size": 0.0},
        pos, _bar(30, 100.0, 99.5, 100.0), _features(1.0),
    )
    assert out == 98.5


def test_trailing_atr_long_after_favorable_move():
    """Peak rises → trail follows."""
    cls = get_stop_adjustment_class("trailing_atr")
    pos = Position(
        quantity=1,
        avg_entry_price=100.0,
        initial_stop_price=98.0,
        current_stop_price=98.0,
        peak_favorable_price=104.0,  # +4 pts favorable
    )
    out = cls.adjusted_stop(
        {"atr_feature_id": "mes_atr_14m", "multiplier": 1.5, "tick_size": 0.0},
        pos, _bar(30, 104.0, 103.5, 103.8), _features(1.0),
    )
    # Candidate = 104 - 1.5 = 102.5; current = 98; ratchets to 102.5
    assert out == 102.5


def test_trailing_atr_long_does_not_loosen():
    """If candidate < current, return None (don't move adversely)."""
    cls = get_stop_adjustment_class("trailing_atr")
    pos = Position(
        quantity=1,
        avg_entry_price=100.0,
        initial_stop_price=98.0,
        current_stop_price=102.5,  # already trailed up
        peak_favorable_price=103.0,  # peak hasn't moved past this
    )
    # Candidate = 103 - 1.5 = 101.5 < current 102.5 → keep current
    out = cls.adjusted_stop(
        {"atr_feature_id": "mes_atr_14m", "multiplier": 1.5, "tick_size": 0.0},
        pos, _bar(30, 103.0, 102.7, 102.8), _features(1.0),
    )
    assert out is None


# ── activation_R behavior ────────────────────────────────────────────


def test_trailing_atr_activation_R_blocks_premature_trail():
    """activation_R=1.0 with init R=2.0 blocks until favorable >= 2.0."""
    cls = get_stop_adjustment_class("trailing_atr")
    pos = Position(
        quantity=1,
        avg_entry_price=100.0,
        initial_stop_price=98.0,  # init R = 2
        current_stop_price=98.0,
        peak_favorable_price=101.0,  # +1 favorable, less than 1.0×2=2
    )
    out = cls.adjusted_stop(
        {
            "atr_feature_id": "mes_atr_14m",
            "multiplier": 1.5,
            "activation_R": 1.0,
            "tick_size": 0.0,
        },
        pos, _bar(30, 101.0, 100.5, 100.8), _features(1.0),
    )
    assert out is None


def test_trailing_atr_activation_R_unblocks_after_threshold():
    cls = get_stop_adjustment_class("trailing_atr")
    pos = Position(
        quantity=1,
        avg_entry_price=100.0,
        initial_stop_price=98.0,  # init R = 2
        current_stop_price=98.0,
        peak_favorable_price=102.5,  # +2.5 favorable, exceeds 1.0×2
    )
    out = cls.adjusted_stop(
        {
            "atr_feature_id": "mes_atr_14m",
            "multiplier": 1.5,
            "activation_R": 1.0,
            "tick_size": 0.0,
        },
        pos, _bar(30, 102.5, 102.0, 102.3), _features(1.0),
    )
    # Candidate = 102.5 - 1.5 = 101.0; current 98; ratchet to 101.0
    assert out == 101.0


# ── short side ────────────────────────────────────────────────────────


def test_trailing_atr_short_after_favorable_move():
    cls = get_stop_adjustment_class("trailing_atr")
    pos = Position(
        quantity=-1,
        avg_entry_price=100.0,
        initial_stop_price=102.0,
        current_stop_price=102.0,
        peak_favorable_price=96.0,  # for short: trough_low, -4 favorable
    )
    out = cls.adjusted_stop(
        {"atr_feature_id": "mes_atr_14m", "multiplier": 1.5, "tick_size": 0.0},
        pos, _bar(30, 96.5, 96.0, 96.3), _features(1.0),
    )
    # Candidate = 96 + 1.5 = 97.5; current 102; ratchet down to 97.5
    assert out == 97.5


def test_trailing_atr_short_does_not_loosen():
    cls = get_stop_adjustment_class("trailing_atr")
    pos = Position(
        quantity=-1,
        avg_entry_price=100.0,
        initial_stop_price=102.0,
        current_stop_price=97.5,  # already trailed down
        peak_favorable_price=96.5,  # peak has retraced upward
    )
    # Candidate = 96.5 + 1.5 = 98.0 > current 97.5 → keep current
    out = cls.adjusted_stop(
        {"atr_feature_id": "mes_atr_14m", "multiplier": 1.5, "tick_size": 0.0},
        pos, _bar(30, 96.5, 96.3, 96.4), _features(1.0),
    )
    assert out is None


# ── degenerate cases ─────────────────────────────────────────────────


def test_trailing_atr_returns_none_when_flat():
    cls = get_stop_adjustment_class("trailing_atr")
    pos = Position(quantity=0)
    out = cls.adjusted_stop(
        {"atr_feature_id": "mes_atr_14m", "multiplier": 1.5},
        pos, _bar(30, 100.0, 99.0, 99.5), _features(1.0),
    )
    assert out is None


def test_trailing_atr_returns_none_when_no_peak_yet():
    cls = get_stop_adjustment_class("trailing_atr")
    pos = Position(
        quantity=1,
        avg_entry_price=100.0,
        initial_stop_price=98.0,
        current_stop_price=98.0,
        peak_favorable_price=None,  # not yet initialized
    )
    out = cls.adjusted_stop(
        {"atr_feature_id": "mes_atr_14m", "multiplier": 1.5},
        pos, _bar(30, 100.0, 99.0, 99.5), _features(1.0),
    )
    assert out is None


def test_trailing_atr_returns_none_when_atr_unavailable():
    """No fallback — same posture as atr_multiple."""
    cls = get_stop_adjustment_class("trailing_atr")
    pos = Position(
        quantity=1,
        avg_entry_price=100.0,
        initial_stop_price=98.0,
        current_stop_price=98.0,
        peak_favorable_price=104.0,
    )
    out = cls.adjusted_stop(
        {"atr_feature_id": "mes_atr_14m", "multiplier": 1.5},
        pos, _bar(30, 104.0, 103.0, 103.5), _features(None),
    )
    assert out is None


def test_position_update_peak_favorable_long():
    pos = Position(quantity=1, avg_entry_price=100.0, peak_favorable_price=None)
    pos.update_peak_favorable(101.5, 100.5)
    assert pos.peak_favorable_price == 101.5
    pos.update_peak_favorable(102.3, 101.0)
    assert pos.peak_favorable_price == 102.3
    pos.update_peak_favorable(101.8, 101.2)  # high lower than peak
    assert pos.peak_favorable_price == 102.3


def test_position_update_peak_favorable_short():
    pos = Position(quantity=-1, avg_entry_price=100.0, peak_favorable_price=None)
    pos.update_peak_favorable(99.5, 99.0)
    assert pos.peak_favorable_price == 99.0
    pos.update_peak_favorable(99.3, 98.5)
    assert pos.peak_favorable_price == 98.5
    pos.update_peak_favorable(99.4, 98.8)  # low higher than trough
    assert pos.peak_favorable_price == 98.5


def test_position_update_peak_favorable_no_op_when_flat():
    pos = Position(quantity=0, peak_favorable_price=None)
    pos.update_peak_favorable(99.0, 98.0)
    assert pos.peak_favorable_price is None
