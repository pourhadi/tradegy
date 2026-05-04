"""Tests for Phase 1 regime-feature transforms (column_select,
rolling_percentile_rank, rolling_change).

Phase 1 of the regime-gated range-scalp plan
(/Users/dan/.claude/plans/brainstorming-in-the-context-humming-balloon.md).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

import tradegy.features.transforms  # noqa: F401  — register transforms
from tradegy.features.transforms import get_transform


def _series(values: list[float]) -> pl.DataFrame:
    """Build a (ts_utc, value) frame from a list of floats."""
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return pl.DataFrame(
        {
            "ts_utc": [base + timedelta(days=i) for i in range(len(values))],
            "value": values,
        }
    )


# ── column_select ────────────────────────────────────────────────────


def test_column_select_projects_named_column():
    fn = get_transform("column_select")
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    src = pl.DataFrame(
        {
            "ts_utc": [base + timedelta(days=i) for i in range(3)],
            "open": [10.0, 11.0, 12.0],
            "close": [10.5, 11.5, 12.5],
        }
    )
    out = fn({"frame": src}, {"column": "close"})
    assert out.columns == ["ts_utc", "value"]
    assert out["value"].to_list() == [10.5, 11.5, 12.5]


def test_column_select_drops_nulls():
    fn = get_transform("column_select")
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    src = pl.DataFrame(
        {
            "ts_utc": [base + timedelta(days=i) for i in range(3)],
            "close": [10.5, None, 12.5],
        }
    )
    out = fn({"frame": src}, {"column": "close"})
    assert out.height == 2
    assert out["value"].to_list() == [10.5, 12.5]


def test_column_select_missing_column_raises():
    fn = get_transform("column_select")
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    src = pl.DataFrame({"ts_utc": [base], "x": [1.0]})
    with pytest.raises(ValueError, match="not in frame columns"):
        fn({"frame": src}, {"column": "close"})


def test_column_select_requires_column_param():
    fn = get_transform("column_select")
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    src = pl.DataFrame({"ts_utc": [base], "value": [1.0]})
    with pytest.raises(ValueError, match="parameter 'column' is required"):
        fn({"frame": src}, {})


# ── rolling_percentile_rank ──────────────────────────────────────────


def test_rolling_percentile_rank_endpoints():
    """Max-of-window scores ~1.0; min-of-window scores ~0.0."""
    fn = get_transform("rolling_percentile_rank")
    # Strictly increasing → every endpoint is the max of its window
    src = _series([1.0, 2.0, 3.0, 4.0, 5.0])
    out = fn({"series": src}, {"window_bars": 3})
    # First 2 rows dropped (window < 3), then ranks 1.0 each
    assert out.height == 3
    assert out["value"].to_list() == [1.0, 1.0, 1.0]


def test_rolling_percentile_rank_decreasing():
    fn = get_transform("rolling_percentile_rank")
    src = _series([5.0, 4.0, 3.0, 2.0, 1.0])
    out = fn({"series": src}, {"window_bars": 3})
    # Every endpoint is the min of its window → 0.0
    assert out.height == 3
    assert out["value"].to_list() == [0.0, 0.0, 0.0]


def test_rolling_percentile_rank_median_of_window():
    """Middle value of a 3-window symmetric set → 0.5."""
    fn = get_transform("rolling_percentile_rank")
    src = _series([1.0, 3.0, 2.0])  # at i=2: window=[1,3,2], current=2 → rank
    out = fn({"series": src}, {"window_bars": 3})
    # window=[1.0, 3.0, 2.0], current=2.0
    # less = 1 (only 1.0 < 2.0); equal = 1 (just self); rank = 1 + 0/2 = 1
    # pct = 1 / (3 - 1) = 0.5
    assert out.height == 1
    assert out["value"].to_list() == [0.5]


def test_rolling_percentile_rank_window_too_small_raises():
    fn = get_transform("rolling_percentile_rank")
    src = _series([1.0, 2.0, 3.0])
    with pytest.raises(ValueError, match=">= 2"):
        fn({"series": src}, {"window_bars": 1})


def test_rolling_percentile_rank_no_finite_outputs_dropped():
    """Verify we drop NaN, not just null — polars distinguishes them."""
    fn = get_transform("rolling_percentile_rank")
    src = _series([1.0, 2.0, 3.0, 4.0])
    out = fn({"series": src}, {"window_bars": 3})
    # 4 input rows, window 3 → 2 valid rows. No NaN should remain.
    assert out.height == 2
    nan_count = out.filter(pl.col("value").is_nan()).height
    assert nan_count == 0


# ── rolling_change ────────────────────────────────────────────────────


def test_rolling_change_abs_lag_1():
    fn = get_transform("rolling_change")
    src = _series([10.0, 12.0, 11.0, 14.0])
    out = fn({"series": src}, {"lag_bars": 1})
    # lag=1: [12-10, 11-12, 14-11] = [2.0, -1.0, 3.0]
    assert out["value"].to_list() == [2.0, -1.0, 3.0]


def test_rolling_change_abs_lag_3():
    fn = get_transform("rolling_change")
    src = _series([10.0, 12.0, 11.0, 14.0, 13.0])
    out = fn({"series": src}, {"lag_bars": 3})
    # lag=3: [14-10, 13-12] = [4.0, 1.0]
    assert out["value"].to_list() == [4.0, 1.0]


def test_rolling_change_pct_mode():
    fn = get_transform("rolling_change")
    src = _series([100.0, 110.0, 99.0])
    out = fn({"series": src}, {"lag_bars": 1, "mode": "pct"})
    # pct = current/lagged - 1 = [110/100 - 1, 99/110 - 1] = [0.1, -0.1]
    vals = out["value"].to_list()
    assert abs(vals[0] - 0.1) < 1e-9
    assert abs(vals[1] - (-0.1)) < 1e-9


def test_rolling_change_pct_mode_handles_zero_lagged():
    fn = get_transform("rolling_change")
    src = _series([0.0, 10.0])
    out = fn({"series": src}, {"lag_bars": 1, "mode": "pct"})
    # pct mode where lagged == 0 → null → dropped
    assert out.height == 0


def test_rolling_change_invalid_mode_raises():
    fn = get_transform("rolling_change")
    src = _series([1.0, 2.0])
    with pytest.raises(ValueError, match="unsupported mode"):
        fn({"series": src}, {"lag_bars": 1, "mode": "unknown"})


def test_rolling_change_lag_zero_raises():
    fn = get_transform("rolling_change")
    src = _series([1.0, 2.0])
    with pytest.raises(ValueError, match=">= 1"):
        fn({"series": src}, {"lag_bars": 0})
