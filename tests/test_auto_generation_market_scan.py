"""Market-structure-monitor tests.

Computation correctness on synthetic frames + cache round-trip +
formatter rendering. We do not rely on production parquets here so
the tests stay fast and independent of the live registry.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from tradegy.auto_generation.market_scan import (
    DEFAULT_BAR_FEATURE,
    DEFAULT_SESSION_FEATURE,
    MarketScanReport,
    Observation,
    SCAN_SCHEMA_VERSION,
    _compute_overnight_gaps,
    _compute_realized_vol_session,
    _compute_session_volume,
    _compute_top_decile_session_position,
    _percentile_rank,
    _split_recent_baseline,
    format_market_scan_report,
    read_latest_market_scan,
    write_market_scan,
)


# ── _percentile_rank ────────────────────────────────────────────


def test_percentile_rank_typical():
    assert _percentile_rank(5.0, [1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(0.8)


def test_percentile_rank_above_max():
    assert _percentile_rank(10.0, [1.0, 2.0, 3.0]) == 1.0


def test_percentile_rank_below_min():
    assert _percentile_rank(0.0, [1.0, 2.0, 3.0]) == 0.0


def test_percentile_rank_empty_distribution_returns_none():
    assert _percentile_rank(1.0, []) is None


# ── _split_recent_baseline ──────────────────────────────────────


def _build_synthetic_bars(n_sessions: int, bars_per_session: int = 10):
    """Produce a synthetic bar frame with `n_sessions` RTH sessions,
    each with `bars_per_session` 1m bars. session_position cycles
    0.0 → 1.0 within each session.
    """
    rows = []
    base_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for sid in range(n_sessions):
        for bi in range(bars_per_session):
            ts = base_ts + timedelta(minutes=sid * 24 * 60 + bi)
            rows.append({
                "ts_utc": ts,
                "open": 100.0 + sid * 0.1 + bi * 0.01,
                "high": 100.0 + sid * 0.1 + bi * 0.01 + 0.05,
                "low": 100.0 + sid * 0.1 + bi * 0.01 - 0.05,
                "close": 100.0 + sid * 0.1 + bi * 0.01 + 0.02,
                "volume": 100 + sid + bi,
                DEFAULT_SESSION_FEATURE: bi / max(1, bars_per_session - 1),
            })
    return pl.DataFrame(rows).with_columns(
        (pl.col(DEFAULT_SESSION_FEATURE) == 0.0).cum_sum().alias("session_id"),
    )


def test_split_recent_baseline_balances_windows():
    bars = _build_synthetic_bars(n_sessions=20, bars_per_session=5)
    recent, baseline = _split_recent_baseline(
        bars,
        session_pos_col=DEFAULT_SESSION_FEATURE,
        recent_sessions=5,
        baseline_sessions=10,
    )
    assert recent["session_id"].n_unique() == 5
    assert baseline["session_id"].n_unique() == 10
    # No overlap.
    overlap = set(recent["session_id"].to_list()) & set(baseline["session_id"].to_list())
    assert overlap == set()


def test_split_clamps_when_short():
    bars = _build_synthetic_bars(n_sessions=3, bars_per_session=5)
    recent, baseline = _split_recent_baseline(
        bars,
        session_pos_col=DEFAULT_SESSION_FEATURE,
        recent_sessions=10,
        baseline_sessions=100,
    )
    # With only 3 sessions, recent gets 3 and baseline gets 0.
    assert recent["session_id"].n_unique() == 3
    assert baseline.is_empty()


# ── Per-metric computations ─────────────────────────────────────


def test_realized_vol_per_session_returns_one_value_each():
    bars = _build_synthetic_bars(n_sessions=4, bars_per_session=10)
    rv = _compute_realized_vol_session(bars)
    assert len(rv) == 4
    assert all(isinstance(x, float) for x in rv)


def test_overnight_gaps_skips_first_session():
    bars = _build_synthetic_bars(n_sessions=4, bars_per_session=5)
    gaps = _compute_overnight_gaps(
        bars, session_pos_col=DEFAULT_SESSION_FEATURE,
    )
    # 4 sessions → 3 overnight gaps.
    assert len(gaps) == 3


def test_top_decile_session_position_within_unit_interval():
    bars = _build_synthetic_bars(n_sessions=4, bars_per_session=10)
    positions = _compute_top_decile_session_position(
        bars, session_pos_col=DEFAULT_SESSION_FEATURE,
    )
    assert all(0.0 <= p <= 1.0 for p in positions)


def test_session_volume_sums_per_session():
    bars = _build_synthetic_bars(n_sessions=3, bars_per_session=4)
    volumes = _compute_session_volume(bars)
    assert len(volumes) == 3
    assert all(v > 0 for v in volumes)


# ── Cache round-trip ────────────────────────────────────────────


def _make_report() -> MarketScanReport:
    obs = (
        Observation(
            metric="realized_vol_per_session",
            current_value=0.05,
            baseline_value=0.04,
            percentile=0.7,
            interpretation="realized vol: recent at p70 of baseline — near baseline median",
        ),
    )
    return MarketScanReport(
        instrument="MES",
        bar_feature="mes_1m_bars",
        session_feature="mes_xnys_session_position",
        recent_sessions=60,
        baseline_sessions=1260,
        recent_window=("2026-01-01T00:00:00+00:00", "2026-04-30T20:00:00+00:00"),
        baseline_window=("2021-01-01T00:00:00+00:00", "2025-12-31T20:00:00+00:00"),
        observations=obs,
        computed_at="2026-05-02T12:00:00+00:00",
    )


def test_write_then_read(tmp_path: Path):
    rep = _make_report()
    path = write_market_scan(rep, root=tmp_path)
    assert path.exists()
    out = read_latest_market_scan(root=tmp_path)
    assert out is not None
    assert out.instrument == "MES"
    assert len(out.observations) == 1
    assert out.observations[0].metric == "realized_vol_per_session"


def test_read_returns_none_when_missing(tmp_path: Path):
    assert read_latest_market_scan(root=tmp_path / "nope") is None


def test_read_returns_none_on_schema_version_mismatch(tmp_path: Path):
    import json

    base = tmp_path
    base.mkdir(parents=True, exist_ok=True)
    (base / "market_scan_20990101000000.json").write_text(
        json.dumps({"schema_version": "999", "observations": []})
    )
    assert read_latest_market_scan(root=base) is None


def test_read_picks_most_recent(tmp_path: Path):
    rep1 = _make_report()
    p1 = write_market_scan(rep1, root=tmp_path)
    # Force a second filename by varying computed_at.
    rep2 = MarketScanReport(
        **{**rep1.__dict__, "computed_at": "2027-01-01T00:00:00+00:00"},
    )
    p2 = write_market_scan(rep2, root=tmp_path)
    assert p1 != p2
    out = read_latest_market_scan(root=tmp_path)
    assert out is not None
    assert out.computed_at == "2027-01-01T00:00:00+00:00"


# ── Rendering ───────────────────────────────────────────────────


def test_format_returns_empty_when_none():
    assert format_market_scan_report(None) == ""


def test_format_renders_observations():
    rep = _make_report()
    out = format_market_scan_report(rep)
    assert "Current market-structure observations" in out
    assert "realized vol: recent at p70" in out


def test_format_returns_empty_when_no_observations():
    rep = MarketScanReport(
        instrument="MES",
        bar_feature="x",
        session_feature="y",
        recent_sessions=10,
        baseline_sessions=100,
        recent_window=("", ""),
        baseline_window=("", ""),
        observations=(),
        computed_at="2026-05-02T12:00:00+00:00",
    )
    assert format_market_scan_report(rep) == ""
