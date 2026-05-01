"""Combinatorial Purged CV harness — fold split + path enumeration + gate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import comb
from pathlib import Path

import polars as pl
import pytest

from tradegy import config
from tradegy.harness import (
    CPCVConfig,
    CPCVPath,
    CPCVSummary,
    enumerate_paths,
    run_cpcv,
    split_folds,
)
from tradegy.harness.cpcv import _evaluate_gate, _populate_distribution
from tradegy.harness.stats import AggregateStats
from tradegy.specs.schema import (
    EntrySpec,
    ExitsSpec,
    MarketScopeSpec,
    MetadataSpec,
    SizingSpec,
    StopsSpec,
    StrategySpec,
    TimeStopBlock,
)


@pytest.fixture(autouse=True)
def _use_production_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override the global conftest's redirection so the harness can
    resolve mes_1m_bars / mes_5m_log_returns YAMLs from the production
    registry. Parquet data still comes from tmp_path via ``feature_root``.
    """
    repo_root = Path(__file__).parent.parent
    monkeypatch.setattr(
        config, "data_sources_registry_dir",
        lambda: repo_root / "registries" / "data_sources",
    )
    monkeypatch.setattr(
        config, "features_registry_dir",
        lambda: repo_root / "registries" / "features",
    )


# ---------- pure helpers (no I/O) ----------


def test_split_folds_produces_equal_width_partitions() -> None:
    cs = datetime(2020, 1, 1, tzinfo=timezone.utc)
    ce = datetime(2024, 1, 1, tzinfo=timezone.utc)
    folds = split_folds(cs, ce, n_folds=4)
    assert len(folds) == 4
    assert folds[0].start == cs
    assert folds[-1].end == ce
    # Folds are contiguous and roughly equal-width (last absorbs remainder).
    for a, b in zip(folds, folds[1:]):
        assert a.end == b.start
    width0 = folds[0].end - folds[0].start
    width1 = folds[1].end - folds[1].start
    assert width0 == width1


def test_split_folds_rejects_inverted_span() -> None:
    cs = datetime(2024, 1, 1, tzinfo=timezone.utc)
    ce = datetime(2020, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="must be after"):
        split_folds(cs, ce, n_folds=4)


def test_enumerate_paths_produces_C_n_k() -> None:
    cs = datetime(2020, 1, 1, tzinfo=timezone.utc)
    ce = datetime(2024, 1, 1, tzinfo=timezone.utc)
    folds = split_folds(cs, ce, n_folds=5)
    paths = enumerate_paths(folds, k_test_folds=2)
    assert len(paths) == comb(5, 2)
    # All path.test_intervals are 2 disjoint windows aligning with folds.
    for p in paths:
        assert len(p.test_intervals) == 2
        idx0, idx1 = p.test_fold_indices
        assert p.test_intervals[0] == (folds[idx0].start, folds[idx0].end)
        assert p.test_intervals[1] == (folds[idx1].start, folds[idx1].end)
    # No duplicate paths.
    seen = {p.test_fold_indices for p in paths}
    assert len(seen) == len(paths)


def test_cpcv_config_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="n_folds must be"):
        CPCVConfig(n_folds=1)
    with pytest.raises(ValueError, match="k_test_folds"):
        CPCVConfig(n_folds=5, k_test_folds=0)
    with pytest.raises(ValueError, match="k_test_folds"):
        CPCVConfig(n_folds=5, k_test_folds=5)
    with pytest.raises(ValueError, match="purge_days"):
        CPCVConfig(purge_days=-1.0)
    with pytest.raises(ValueError, match="max_pct_paths_negative"):
        CPCVConfig(max_pct_paths_negative=1.5)


def _summary_with_paths(sharpes: list[float | None]) -> CPCVSummary:
    """Build a CPCVSummary with synthesized path stats; ``None`` means
    the path produced no trades."""
    paths: list[CPCVPath] = []
    for i, s in enumerate(sharpes):
        p = CPCVPath(index=i, test_fold_indices=(i,), test_intervals=[])
        if s is None:
            p.stats = AggregateStats(
                total_trades=0, expectancy_R=0.0, total_pnl=0.0,
                total_pnl_R=0.0, win_rate=0.0, avg_win_R=0.0,
                avg_loss_R=0.0, profit_factor=0.0, avg_holding_bars=0.0,
                sharpe=0.0, max_drawdown=0.0,
            )
        else:
            p.stats = AggregateStats(
                total_trades=10, expectancy_R=0.1, total_pnl=100.0,
                total_pnl_R=1.0, win_rate=0.5, avg_win_R=1.0,
                avg_loss_R=-0.5, profit_factor=2.0, avg_holding_bars=5.0,
                sharpe=s, max_drawdown=10.0,
            )
        paths.append(p)
    return CPCVSummary(
        spec_id="x", spec_version="1.0",
        config=CPCVConfig(n_folds=4, k_test_folds=2),
        coverage_start=datetime(2020, 1, 1, tzinfo=timezone.utc),
        coverage_end=datetime(2024, 1, 1, tzinfo=timezone.utc),
        paths=paths,
    )


def test_distribution_excludes_paths_with_no_trades() -> None:
    s = _summary_with_paths([0.5, 1.0, None, 1.5, -0.2])
    _populate_distribution(s)
    # 4 paths produced trades, 1 did not.
    assert s.paths_with_trades == 4
    assert s.median_sharpe == pytest.approx(0.75)  # median of [-0.2, 0.5, 1.0, 1.5]
    # 1 of 4 paths is negative.
    assert s.pct_paths_negative == pytest.approx(0.25)


def test_gate_passes_when_median_above_threshold_and_few_negatives() -> None:
    s = _summary_with_paths([0.9, 1.0, 1.5, 2.0])
    _populate_distribution(s)
    s.config = CPCVConfig(
        n_folds=4, k_test_folds=2,
        median_sharpe_threshold=0.8, max_pct_paths_negative=0.20,
    )
    passed, reason = _evaluate_gate(s)
    assert passed, reason


def test_gate_fails_when_median_below_threshold() -> None:
    s = _summary_with_paths([0.1, 0.2, 0.3, 0.4])
    _populate_distribution(s)
    s.config = CPCVConfig(
        n_folds=4, k_test_folds=2,
        median_sharpe_threshold=0.8, max_pct_paths_negative=0.20,
    )
    passed, reason = _evaluate_gate(s)
    assert not passed
    assert "median Sharpe" in reason


def test_gate_fails_when_too_many_paths_negative() -> None:
    # 6 paths, 2 negative = 33% > 20%, but median (1.0) is fine.
    s = _summary_with_paths([1.0, 1.0, 1.0, 1.0, -0.1, -0.5])
    _populate_distribution(s)
    s.config = CPCVConfig(
        n_folds=4, k_test_folds=2,
        median_sharpe_threshold=0.8, max_pct_paths_negative=0.20,
    )
    passed, reason = _evaluate_gate(s)
    assert not passed
    assert "pct paths negative" in reason


def test_gate_fails_when_no_path_produced_trades() -> None:
    s = _summary_with_paths([None, None])
    _populate_distribution(s)
    passed, reason = _evaluate_gate(s)
    assert not passed
    assert "no path produced trades" in reason


# ---------- end-to-end on synthetic data ----------


def _build_synth_data(tmp_path: Path) -> Path:
    """3-year synthetic mes_1m_bars + mes_5m_log_returns, daily granularity.

    Mirrors the helper in test_walk_forward.py — coarse daily bars are
    enough to exercise the CPCV split + per-path eval without a heavy
    1m series.
    """
    feat_root = tmp_path / "features"
    bars_dir = feat_root / "feature=mes_1m_bars" / "version=v1"
    rets_dir = feat_root / "feature=mes_5m_log_returns" / "version=v1"

    rows: list[dict] = []
    rets: list[dict] = []
    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    day = base
    n = 0
    while n < 750:
        if day.weekday() < 5:
            ts = day.replace(hour=14, minute=30)
            close = 5000.0 + (n % 50) * 0.5
            rows.append({
                "ts_utc": ts,
                "open": close - 0.25, "high": close + 0.5, "low": close - 0.5,
                "close": close,
                "volume": 100, "num_trades": 50,
                "bid_volume": 60, "ask_volume": 40,
            })
            rets.append({"ts_utc": ts, "value": 0.005})
            n += 1
        day = day + timedelta(days=1)

    bars_df = pl.DataFrame(rows, schema={
        "ts_utc": pl.Datetime("ns", "UTC"),
        "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Int64, "num_trades": pl.Int64,
        "bid_volume": pl.Int64, "ask_volume": pl.Int64,
    })
    rets_df = pl.DataFrame(rets, schema={
        "ts_utc": pl.Datetime("ns", "UTC"), "value": pl.Float64,
    })

    bd = bars_dir / "date=2020-01-01"
    bd.mkdir(parents=True)
    bars_df.write_parquet(bd / "data.parquet")
    rd = rets_dir / "date=2020-01-01"
    rd.mkdir(parents=True)
    rets_df.write_parquet(rd / "data.parquet")
    return feat_root


def _build_spec() -> StrategySpec:
    return StrategySpec(
        metadata=MetadataSpec(
            id="cpcv_test", version="0.1.0", schema_version="1.0",
            name="CPCV Test", status="draft",
            created_date="2026-04-30", last_modified_date="2026-04-30",
            author="test",
        ),
        market_scope=MarketScopeSpec(instrument="MES", session="globex"),
        entry=EntrySpec(
            strategy_class="momentum_breakout",
            parameters={
                "return_feature_id": "mes_5m_log_returns",
                "entry_threshold": 0.001,
                "max_attempts_per_session": 1,
            },
            direction="long",
            entry_order_type="market",
        ),
        sizing=SizingSpec(method="fixed_contracts", parameters={"contracts": 1}),
        stops=StopsSpec(
            initial_stop={"method": "fixed_ticks", "stop_ticks": 8, "tick_size": 0.25},
            hard_max_distance_ticks=100,
            time_stop=TimeStopBlock(enabled=True, max_holding_bars=10),
        ),
        exits=ExitsSpec(invalidation_conditions=[]),
    )


def test_cpcv_runs_end_to_end_and_populates_summary(tmp_path: Path) -> None:
    feat_root = _build_synth_data(tmp_path)
    spec = _build_spec()
    cfg = CPCVConfig(n_folds=4, k_test_folds=2)
    summary = run_cpcv(
        spec,
        coverage_start=datetime(2020, 1, 1, tzinfo=timezone.utc),
        coverage_end=datetime(2023, 1, 1, tzinfo=timezone.utc),
        config=cfg,
        feature_root=feat_root,
    )
    # C(4, 2) = 6 paths.
    assert len(summary.paths) == comb(4, 2)
    assert all(p.stats is not None for p in summary.paths)
    # Each path's trade list matches its stats.total_trades.
    for p in summary.paths:
        assert len(p.trades) == p.stats.total_trades
    # Distribution was populated for any paths with trades.
    if summary.paths_with_trades:
        assert isinstance(summary.median_sharpe, float)
        assert 0.0 <= summary.pct_paths_negative <= 1.0
