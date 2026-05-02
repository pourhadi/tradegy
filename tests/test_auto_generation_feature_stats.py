"""Feature-stats module tests.

Computation correctness against synthetic frames + cache round-trip
+ rendering. We don't rely on production feature parquets here so
the tests stay fast and independent of the live registry.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from tradegy.auto_generation.feature_stats import (
    FeatureStats,
    _compute_from_dataframe,
    compute_feature_stats,
    format_feature_stats,
    read_stats,
    write_stats,
)


# ── Computation ────────────────────────────────────────────────


def test_compute_picks_value_column():
    df = pl.DataFrame({
        "ts_utc": [datetime(2026, 5, 1, tzinfo=timezone.utc)] * 5,
        "value": [0.1, 0.2, 0.3, 0.4, 0.5],
    })
    stats = _compute_from_dataframe("demo", df)
    assert stats.rows == 5
    assert stats.min == 0.1
    assert stats.max == 0.5
    assert abs(stats.median - 0.3) < 1e-9


def test_compute_falls_back_to_close_for_bars():
    df = pl.DataFrame({
        "ts_utc": [datetime(2026, 5, 1, tzinfo=timezone.utc)] * 4,
        "open": [10.0, 11.0, 12.0, 13.0],
        "high": [10.5, 11.5, 12.5, 13.5],
        "low": [9.5, 10.5, 11.5, 12.5],
        "close": [10.2, 11.2, 12.2, 13.2],
        "volume": [100, 200, 300, 400],
    })
    stats = _compute_from_dataframe("bars", df)
    assert "close" in stats.note
    assert abs(stats.min - 10.2) < 1e-9
    assert abs(stats.max - 13.2) < 1e-9


def test_compute_handles_empty_dataframe():
    df = pl.DataFrame({"ts_utc": [], "value": []}, schema={
        "ts_utc": pl.Datetime("ns", "UTC"),
        "value": pl.Float64,
    })
    stats = _compute_from_dataframe("demo", df)
    assert stats.rows == 0
    assert stats.median is None
    assert "zero non-null rows" in stats.note


def test_compute_no_numeric_column():
    df = pl.DataFrame({
        "ts_utc": [datetime(2026, 5, 1, tzinfo=timezone.utc)],
        "label": ["a"],
    })
    stats = _compute_from_dataframe("demo", df)
    assert stats.median is None
    assert "no numeric column" in stats.note


def test_compute_quantiles_match_expected():
    df = pl.DataFrame({
        "ts_utc": [datetime(2026, 5, 1, tzinfo=timezone.utc)] * 100,
        "value": list(range(100)),
    })
    stats = _compute_from_dataframe("demo", df)
    assert stats.rows == 100
    assert abs(stats.p10 - 9.9) < 1.0
    assert abs(stats.p90 - 89.1) < 1.0
    assert abs(stats.median - 49.5) < 1.0


# ── Cache round-trip ───────────────────────────────────────────


def test_write_then_read(tmp_path: Path):
    stats = FeatureStats(
        feature_id="demo",
        rows=10,
        p10=0.1, p25=0.2, median=0.5, p75=0.7, p90=0.9,
        min=0.0, max=1.0,
        computed_at="2026-05-02T00:00:00+00:00",
    )
    write_stats(stats, root=tmp_path)
    out = read_stats("demo", root=tmp_path)
    assert out is not None
    assert out.feature_id == "demo"
    assert out.rows == 10


def test_read_returns_none_when_missing(tmp_path: Path):
    assert read_stats("nope", root=tmp_path) is None


def test_read_returns_none_on_schema_version_mismatch(tmp_path: Path):
    import json

    p = tmp_path / "feature_stats" / "demo.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"feature_id": "demo", "schema_version": "999"}))
    out = read_stats("demo", root=tmp_path)
    assert out is None  # cache miss; caller will recompute


# ── compute_feature_stats integration (via cache miss + write) ───


def test_compute_feature_stats_handles_missing_feature(tmp_path: Path):
    """A feature without a parquet emits a not-materialised note."""
    stats = compute_feature_stats(
        "definitely_does_not_exist_feature",
        feature_root=tmp_path,
        cache_root=tmp_path,
    )
    assert stats.rows == 0
    assert stats.note in (
        "not_materialised",
        "read_feature_raised:FileNotFoundError",
    )


# ── Rendering ─────────────────────────────────────────────────


def test_format_available_stats():
    stats = FeatureStats(
        feature_id="mes_atr_14m",
        rows=2_458_705,
        p10=0.45, p25=0.7, median=1.1, p75=1.8, p90=3.0,
        min=0.0, max=63.4,
        computed_at="2026-05-02T00:00:00+00:00",
    )
    s = format_feature_stats(stats)
    assert "mes_atr_14m" in s
    assert "rows=2,458,705" in s
    assert "median=1.1" in s
    assert "p10=" in s and "p90=" in s


def test_format_unavailable_stats_shows_note():
    stats = FeatureStats(
        feature_id="mes_demo",
        rows=0,
        p10=None, p25=None, median=None, p75=None, p90=None,
        min=None, max=None,
        computed_at="2026-05-02T00:00:00+00:00",
        note="not_materialised",
    )
    s = format_feature_stats(stats)
    assert "mes_demo" in s
    assert "not yet materialised" in s
    assert "not_materialised" in s


def test_is_available_property():
    avail = FeatureStats(
        feature_id="x", rows=10,
        p10=0.0, p25=0.0, median=0.5, p75=1.0, p90=1.0,
        min=0.0, max=1.0,
        computed_at="x",
    )
    assert avail.is_available is True

    unavail = FeatureStats(
        feature_id="y", rows=0,
        p10=None, p25=None, median=None, p75=None, p90=None,
        min=None, max=None,
        computed_at="x",
    )
    assert unavail.is_available is False
