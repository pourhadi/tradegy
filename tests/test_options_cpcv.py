"""CPCV harness tests for the options runner.

Real-data integration test against ingested SPX chain. Pure-math/
state tests for split_folds, enumerate_paths, _evaluate_gate, and
_populate_distribution.
"""
from __future__ import annotations

from datetime import datetime, date
from itertools import combinations

import pytest

from tradegy import config
from tradegy.options.cpcv import (
    OptionsCPCVConfig,
    OptionsCPCVPath,
    OptionsCPCVSummary,
    OptionsFoldBoundary,
    _evaluate_gate,
    _populate_distribution,
    enumerate_paths,
    run_options_cpcv,
    split_folds,
)
from tradegy.options.runner import ClosedTrade
from tradegy.options.strategies import PutCreditSpread45dteD30


# ── split_folds: pure date math ─────────────────────────────────────


def test_split_folds_equal_width_with_remainder_in_last():
    """7-day span / 3 folds → folds of width 2.33 days. Last fold
    absorbs rounding so it lands exactly on coverage_end.
    """
    folds = split_folds(
        coverage_start=datetime(2024, 1, 1),
        coverage_end=datetime(2024, 1, 8),
        n_folds=3,
    )
    assert len(folds) == 3
    assert folds[0].index == 0
    assert folds[0].start == datetime(2024, 1, 1)
    assert folds[-1].end == datetime(2024, 1, 8)
    # Folds chain end-to-start without gaps.
    for prev, nxt in zip(folds[:-1], folds[1:]):
        assert prev.end == nxt.start


def test_split_folds_invalid_coverage_rejects():
    with pytest.raises(ValueError):
        split_folds(
            coverage_start=datetime(2024, 1, 2),
            coverage_end=datetime(2024, 1, 1),
            n_folds=3,
        )


def test_split_folds_n2_minimum():
    folds = split_folds(
        coverage_start=datetime(2024, 1, 1),
        coverage_end=datetime(2024, 12, 31),
        n_folds=2,
    )
    assert len(folds) == 2


# ── enumerate_paths: combinatorial enumeration ──────────────────────


def test_enumerate_paths_c10_2_produces_45_paths():
    """C(10, 2) = 45 paths."""
    folds = split_folds(
        coverage_start=datetime(2020, 1, 1),
        coverage_end=datetime(2026, 1, 1),
        n_folds=10,
    )
    paths = enumerate_paths(folds, k_test_folds=2)
    assert len(paths) == 45
    # Path indices unique and dense.
    assert {p.index for p in paths} == set(range(45))
    # Each path picks 2 distinct fold indices.
    for p in paths:
        assert len(p.test_fold_indices) == 2
        assert len(set(p.test_fold_indices)) == 2


def test_enumerate_paths_c5_3_produces_10():
    """C(5, 3) = 10."""
    folds = split_folds(
        coverage_start=datetime(2024, 1, 1),
        coverage_end=datetime(2025, 1, 1),
        n_folds=5,
    )
    paths = enumerate_paths(folds, k_test_folds=3)
    assert len(paths) == 10


# ── OptionsCPCVConfig validation ────────────────────────────────────


def test_config_rejects_n_folds_below_2():
    with pytest.raises(ValueError):
        OptionsCPCVConfig(n_folds=1, k_test_folds=1)


def test_config_rejects_k_outside_range():
    with pytest.raises(ValueError):
        OptionsCPCVConfig(n_folds=10, k_test_folds=10)
    with pytest.raises(ValueError):
        OptionsCPCVConfig(n_folds=10, k_test_folds=0)


def test_config_rejects_negative_purge_embargo():
    with pytest.raises(ValueError):
        OptionsCPCVConfig(purge_days=-1.0)


# ── _populate_distribution: distribution math ───────────────────────


def _make_path(index: int, sharpe: float, n_trades: int = 1) -> OptionsCPCVPath:
    p = OptionsCPCVPath(
        index=index,
        test_fold_indices=(index,),
        test_intervals=[(datetime(2024, 1, 1), datetime(2024, 2, 1))],
    )
    p.trades = [
        ClosedTrade(
            position_id=f"x{index}",
            strategy_class="x",
            contracts=1,
            entry_ts=datetime(2024, 1, 1),
            closed_ts=datetime(2024, 1, 2),
            entry_credit_per_share=0.0,
            closed_credit_per_share=0.0,
            closed_pnl_per_share=0.0,
            closed_pnl_dollars=0.0,
            open_commission=0.0,
            close_commission=0.0,
            closed_reason="x",
            expiries=(date(2024, 2, 1),),
        )
        for _ in range(n_trades)
    ]
    p.sharpe = sharpe
    return p


def test_distribution_median_iqr_pct_negative():
    """Sharpes [-0.5, -0.2, 0.1, 0.4, 0.7, 1.0, 1.3] → median = 0.4,
    pct_negative = 2/7 ≈ 28.6%.
    """
    sharpes = [-0.5, -0.2, 0.1, 0.4, 0.7, 1.0, 1.3]
    paths = [_make_path(i, s) for i, s in enumerate(sharpes)]
    summary = OptionsCPCVSummary(
        strategy_ids=("x",),
        config=OptionsCPCVConfig(),
        coverage_start=datetime(2024, 1, 1),
        coverage_end=datetime(2025, 1, 1),
        paths=paths,
    )
    _populate_distribution(summary)
    assert summary.paths_with_trades == 7
    assert summary.median_sharpe == pytest.approx(0.4)
    assert summary.pct_paths_negative == pytest.approx(2 / 7)
    assert summary.iqr_sharpe > 0


def test_distribution_zero_paths_with_trades():
    """No path produced trades → distribution stays at 0; the gate
    will fail with 'never fired'.
    """
    p = OptionsCPCVPath(
        index=0,
        test_fold_indices=(0,),
        test_intervals=[(datetime(2024, 1, 1), datetime(2024, 2, 1))],
    )
    # No trades appended.
    summary = OptionsCPCVSummary(
        strategy_ids=("x",),
        config=OptionsCPCVConfig(),
        coverage_start=datetime(2024, 1, 1),
        coverage_end=datetime(2025, 1, 1),
        paths=[p],
    )
    _populate_distribution(summary)
    assert summary.paths_with_trades == 0
    assert summary.median_sharpe == 0.0


# ── _evaluate_gate: gate logic ──────────────────────────────────────


def _summary(
    median: float, pct_neg: float, paths_with_trades: int = 10,
    cfg: OptionsCPCVConfig | None = None,
) -> OptionsCPCVSummary:
    s = OptionsCPCVSummary(
        strategy_ids=("x",),
        config=cfg or OptionsCPCVConfig(),
        coverage_start=datetime(2024, 1, 1),
        coverage_end=datetime(2025, 1, 1),
    )
    s.median_sharpe = median
    s.pct_paths_negative = pct_neg
    s.paths_with_trades = paths_with_trades
    return s


def test_gate_passes_at_thresholds():
    """median = 0.8 (exactly threshold) and pct_neg = 0.20 (exactly
    threshold) — both edges should pass.
    """
    s = _summary(median=0.8, pct_neg=0.20)
    passed, reason = _evaluate_gate(s)
    assert passed is True
    assert reason == ""


def test_gate_fails_below_median_threshold():
    s = _summary(median=0.79, pct_neg=0.10)
    passed, reason = _evaluate_gate(s)
    assert passed is False
    assert "median Sharpe" in reason


def test_gate_fails_above_pct_negative_threshold():
    s = _summary(median=1.0, pct_neg=0.21)
    passed, reason = _evaluate_gate(s)
    assert passed is False
    assert "paths negative" in reason


def test_gate_fails_when_strategy_never_fires():
    s = _summary(median=0.0, pct_neg=0.0, paths_with_trades=0)
    passed, reason = _evaluate_gate(s)
    assert passed is False
    assert "never fired" in reason


# ── Integration: real SPX CPCV over a small slice ──────────────────


def test_cpcv_runs_against_real_spx_chain(real_spx_chain_snapshots):
    """End-to-end: 4-fold CPCV with k=2 → 6 paths, over a 1-year SPX
    slice. Verifies the full pipeline (fold split → per-fold backtest
    → path concatenation → distribution → gate).

    Small N=4 to keep test runtime under a minute — we still need
    enough paths (C(4, 2) = 6) for distribution math to be meaningful.
    """
    raw_root = config.repo_root() / "data" / "raw"
    cfg = OptionsCPCVConfig(n_folds=4, k_test_folds=2)
    summary = run_options_cpcv(
        strategies=[PutCreditSpread45dteD30()],
        source_id="spx_options_chain",
        ticker="SPX",
        coverage_start=datetime(2020, 1, 1),
        coverage_end=datetime(2021, 1, 1),
        config=cfg,
        root=raw_root,
    )
    assert len(summary.folds) == 4
    assert len(summary.paths) == 6  # C(4, 2)
    # PCS over a year of SPX should produce trades on most fold
    # combinations. Asserting ≥ 1 path with trades exercises the
    # distribution path; not asserting pass/fail of the gate (that
    # is the substantive question this harness exists to answer).
    assert summary.paths_with_trades >= 1
    assert isinstance(summary.passed, bool)
