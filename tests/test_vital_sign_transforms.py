"""Unit tests for the vital-signs transforms.

Each test uses a small fixture frame so the math can be verified by hand
against the transform's contract.
"""
from __future__ import annotations

from datetime import datetime, timezone

import polars as pl
import pytest

from tradegy.features.transforms import get_transform


def _utc(year, month, day, hour, minute, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _bars(rows: list[tuple]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_utc": [r[0] for r in rows],
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
            "volume": [r[5] for r in rows],
        },
        schema={
            "ts_utc": pl.Datetime("ns", "UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
    )


# ----- true_range -----


def test_true_range_handles_overnight_gap() -> None:
    # Bar 1: high=101, low=99, close=100. No prev_close → TR = high-low = 2.
    # Bar 2: high=102, low=100, close=101. prev_close=100. TR = max(2, |102-100|, |100-100|) = 2.
    # Bar 3: gap up — high=110, low=108, close=109. prev_close=101. TR = max(2, |110-101|=9, |108-101|=7) = 9.
    rows = [
        (_utc(2024, 6, 3, 14, 30), 100, 101,  99, 100, 50),
        (_utc(2024, 6, 3, 14, 31), 101, 102, 100, 101, 60),
        (_utc(2024, 6, 3, 14, 32), 108, 110, 108, 109, 70),
    ]
    fn = get_transform("true_range")
    out = fn({"bars": _bars(rows)}, {})
    assert out.height == 3
    vals = out.get_column("value").to_list()
    assert vals == [2.0, 2.0, 9.0]


def test_true_range_missing_column_raises() -> None:
    df = pl.DataFrame({"ts_utc": [_utc(2024, 1, 1, 0, 0)], "high": [1.0]})
    fn = get_transform("true_range")
    with pytest.raises(ValueError, match="missing columns"):
        fn({"bars": df}, {})


# ----- rolling_mean -----


def test_rolling_mean_basic() -> None:
    df = pl.DataFrame(
        {
            "ts_utc": [_utc(2024, 6, 3, 14, m) for m in range(5)],
            "value": [1.0, 2.0, 3.0, 4.0, 5.0],
        },
        schema={"ts_utc": pl.Datetime("ns", "UTC"), "value": pl.Float64},
    )
    fn = get_transform("rolling_mean")
    out = fn({"series": df}, {"window_bars": 3})
    # First 2 rows dropped (window not yet full).
    assert out.height == 3
    assert out.get_column("value").to_list() == [2.0, 3.0, 4.0]


def test_rolling_mean_alternate_column() -> None:
    rows = [
        (_utc(2024, 6, 3, 14, m), 0, 0, 0, 0, v)
        for m, v in enumerate([10, 20, 30])
    ]
    fn = get_transform("rolling_mean")
    out = fn({"series": _bars(rows)}, {"window_bars": 2, "column": "volume"})
    assert out.height == 2
    assert out.get_column("value").to_list() == [15.0, 25.0]


# ----- rolling_zscore -----


def test_rolling_zscore_constant_series_drops_zero_std_rows() -> None:
    df = pl.DataFrame(
        {
            "ts_utc": [_utc(2024, 6, 3, 14, m) for m in range(4)],
            "value": [5.0, 5.0, 5.0, 5.0],
        },
        schema={"ts_utc": pl.Datetime("ns", "UTC"), "value": pl.Float64},
    )
    fn = get_transform("rolling_zscore")
    out = fn({"series": df}, {"window_bars": 3})
    assert out.height == 0  # all rows have std == 0 → dropped


def test_rolling_zscore_known_values() -> None:
    # Window 3 over [1, 2, 3, 4]:
    # Row at idx 2: window=[1,2,3], mean=2, std=1 (sample), z = (3-2)/1 = 1.0
    # Row at idx 3: window=[2,3,4], mean=3, std=1, z = (4-3)/1 = 1.0
    df = pl.DataFrame(
        {
            "ts_utc": [_utc(2024, 6, 3, 14, m) for m in range(4)],
            "value": [1.0, 2.0, 3.0, 4.0],
        },
        schema={"ts_utc": pl.Datetime("ns", "UTC"), "value": pl.Float64},
    )
    fn = get_transform("rolling_zscore")
    out = fn({"series": df}, {"window_bars": 3})
    assert out.height == 2
    vals = out.get_column("value").to_list()
    for v in vals:
        assert abs(v - 1.0) < 1e-9


def test_rolling_zscore_volume_column() -> None:
    rows = [
        (_utc(2024, 6, 3, 14, m), 0, 0, 0, 0, v)
        for m, v in enumerate([100, 100, 100, 200])
    ]
    fn = get_transform("rolling_zscore")
    out = fn({"series": _bars(rows)}, {"window_bars": 3, "column": "volume"})
    # First 2 rows dropped (window not yet full).
    # idx 2: window=[100,100,100], std=0 → null → dropped.
    # idx 3: window=[100,100,200], mean=133.33, std≈57.74, z ≈ (200-133.33)/57.74 ≈ 1.155
    assert out.height == 1
    assert abs(out.get_column("value").item() - (200 - 400/3) / pl.Series([100.0, 100.0, 200.0]).std()) < 1e-9


# ----- session_position -----


def test_session_position_inside_cmes_session() -> None:
    # CMES session 2024-06-03 opens 2024-06-02 22:00 UTC, closes 2024-06-03 22:00 UTC.
    # Total span = 24h. A bar at the midpoint should give value ≈ 0.5.
    rows = [
        (_utc(2024, 6, 2, 22, 0),  0, 0, 0, 0, 0),  # session open: value ≈ 0
        (_utc(2024, 6, 3, 10, 0),  0, 0, 0, 0, 0),  # midpoint: ~0.5
        (_utc(2024, 6, 3, 21, 59), 0, 0, 0, 0, 0),  # near close: ~1.0
    ]
    fn = get_transform("session_position")
    out = fn({"bars": _bars(rows)}, {"session_calendar": "CMES"})
    assert out.height == 3
    vals = out.get_column("value").to_list()
    assert vals[0] == pytest.approx(0.0, abs=1e-6)
    assert vals[1] == pytest.approx(0.5, abs=1e-3)
    assert vals[2] == pytest.approx(1.0, abs=1e-3)


def test_session_position_drops_rows_outside_any_session() -> None:
    # 2024-06-08 is a Saturday — no CMES session. Should be dropped.
    rows = [
        (_utc(2024, 6, 8, 12, 0), 0, 0, 0, 0, 0),
    ]
    fn = get_transform("session_position")
    out = fn({"bars": _bars(rows)}, {"session_calendar": "CMES"})
    assert out.height == 0
