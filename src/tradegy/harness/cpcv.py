"""Combinatorial Purged Cross-Validation for the backtest harness.

Per ``trading_platform_docs/05_backtest_harness.md:334-343``:

  - Divide data into ``N`` folds (default 10).
  - Generate multiple backtest paths by assigning folds to train vs
    test in different combinations.
  - **Purge** a buffer of bars around each test fold to eliminate
    label leakage.
  - **Embargo** a post-test period from future training.
  - Run the strategy on each path.
  - Output: distribution of Sharpes across paths, median, IQR,
    pct paths negative.

  Gate (doc 05:343, configurable): commonly
  ``median Sharpe > 0.8 AND pct paths negative < 20%``.

This module implements that for our deterministic spec-evaluation
harness. A "path" here is a unique choice of ``k`` test folds out of
``N`` (i.e. ``C(N, k)`` paths). Per path we run the spec across each
test fold's bar window via ``run_backtest`` and concatenate the
resulting trades — same parameters everywhere — then compute aggregate
stats from the concatenation. The cross-path distribution gives the
Sharpe summary statistics the doc calls for.

What this MVP does NOT do (filed as known-deferred, mirroring the
walk-forward MVP's deferrals at ``walk_forward.py:19-26``):

  - Within-train parameter optimization. Same multi-testing concern as
    walk-forward; would need Deflated Sharpe correction to be
    interpretable.
  - Active use of ``purge_days`` / ``embargo_days``. The parameters
    are present in the config (forward-compatible) but no-op for now
    because we evaluate frozen parameters on test folds only — there
    is no train-side data to purge from. Purging activates the moment
    a fitting step is added.
  - Deflated Sharpe Ratio. Doc 07's auto-gen prerequisite, not
    required by the basic CPCV gate.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import combinations
from pathlib import Path

from tradegy.harness.execution import CostModel
from tradegy.harness.runner import run_backtest
from tradegy.harness.stats import AggregateStats, aggregate_trades
from tradegy.specs.schema import StrategySpec
from tradegy.strategies.types import Trade


@dataclass(frozen=True)
class CPCVConfig:
    """Combinatorial Purged Cross-Validation configuration.

    Defaults track doc 05:336 (``N=10``) and doc 05:343 thresholds
    (``median > 0.8``, ``< 20%`` paths negative).

    Args:
        n_folds: equal-width folds over the coverage span. Must be ≥ 2.
        k_test_folds: how many folds make up each path's OOS sample.
            Must be in ``[1, n_folds - 1]``. Total paths = ``C(N, k)``.
        purge_days: bars within this many days of any test fold are
            dropped from the (notional) train side. No-op while we
            evaluate frozen parameters; activates when a fitting step
            is introduced. Forward-compatible field.
        embargo_days: same semantic as ``purge_days`` but for the
            forward side of each test fold.
        median_sharpe_threshold: gate — minimum acceptable median
            Sharpe across paths.
        max_pct_paths_negative: gate — maximum acceptable share of
            paths with negative Sharpe (over paths that produced
            trades).
    """

    n_folds: int = 10
    k_test_folds: int = 2
    purge_days: float = 0.0
    embargo_days: float = 0.0
    median_sharpe_threshold: float = 0.8
    max_pct_paths_negative: float = 0.20

    def __post_init__(self) -> None:
        if self.n_folds < 2:
            raise ValueError("n_folds must be ≥ 2")
        if not (1 <= self.k_test_folds < self.n_folds):
            raise ValueError(
                f"k_test_folds must be in [1, n_folds-1]; got "
                f"{self.k_test_folds} with n_folds={self.n_folds}"
            )
        if self.purge_days < 0 or self.embargo_days < 0:
            raise ValueError("purge_days and embargo_days must be ≥ 0")
        if not (0.0 <= self.max_pct_paths_negative <= 1.0):
            raise ValueError("max_pct_paths_negative must be in [0, 1]")


@dataclass(frozen=True)
class FoldBoundary:
    index: int
    start: datetime
    end: datetime


@dataclass
class CPCVPath:
    """One combinatorial path. ``test_fold_indices`` enumerates which
    folds make up this path's OOS sample; ``test_intervals`` is the
    same in (start, end) form. ``trades`` is the concatenation of
    trades produced when ``run_backtest`` is invoked on each fold's
    window in turn."""

    index: int
    test_fold_indices: tuple[int, ...]
    test_intervals: list[tuple[datetime, datetime]]
    trades: list[Trade] = field(default_factory=list)
    stats: AggregateStats | None = None


@dataclass
class CPCVSummary:
    spec_id: str
    spec_version: str
    config: CPCVConfig
    coverage_start: datetime
    coverage_end: datetime
    folds: list[FoldBoundary] = field(default_factory=list)
    paths: list[CPCVPath] = field(default_factory=list)
    # Distribution stats over paths that produced ≥ 1 trade.
    paths_with_trades: int = 0
    median_sharpe: float = 0.0
    iqr_sharpe: float = 0.0
    pct_paths_negative: float = 0.0
    # Gate result.
    passed: bool = False
    fail_reason: str = ""


def split_folds(
    coverage_start: datetime,
    coverage_end: datetime,
    n_folds: int,
) -> list[FoldBoundary]:
    """Cut ``[coverage_start, coverage_end)`` into ``n_folds`` equal-width
    folds. Last fold absorbs any rounding remainder so it always reaches
    ``coverage_end``."""
    if coverage_end <= coverage_start:
        raise ValueError("coverage_end must be after coverage_start")
    span = coverage_end - coverage_start
    width = span / n_folds
    folds: list[FoldBoundary] = []
    for i in range(n_folds):
        start = coverage_start + width * i
        end = coverage_start + width * (i + 1) if i < n_folds - 1 else coverage_end
        folds.append(FoldBoundary(index=i, start=start, end=end))
    return folds


def enumerate_paths(
    folds: list[FoldBoundary], k_test_folds: int
) -> list[CPCVPath]:
    """Produce the ``C(N, k)`` combinatorial paths."""
    paths: list[CPCVPath] = []
    for idx, combo in enumerate(combinations(range(len(folds)), k_test_folds)):
        intervals = [(folds[i].start, folds[i].end) for i in combo]
        paths.append(
            CPCVPath(
                index=idx,
                test_fold_indices=combo,
                test_intervals=intervals,
            )
        )
    return paths


def run_cpcv(
    spec: StrategySpec,
    *,
    coverage_start: datetime,
    coverage_end: datetime,
    config: CPCVConfig | None = None,
    cost: CostModel | None = None,
    feature_root: Path | None = None,
    session_calendar: str = "CMES",
) -> CPCVSummary:
    """Run combinatorial purged CV over the coverage span and produce a
    summary with the distribution gate applied.

    Per-path procedure:

      1. For each test fold in the path, call ``run_backtest`` with
         the fold's ``[start, end)`` window.
      2. Concatenate the resulting trades into the path's trade list.
      3. Compute ``aggregate_trades`` on the concatenation to get the
         path's Sharpe + summary stats.

    Cross-path procedure: collect Sharpes from paths that produced ≥ 1
    trade, compute median / IQR / pct-negative, and apply the gate.
    """
    cfg = config or CPCVConfig()
    cost = cost or CostModel()
    folds = split_folds(coverage_start, coverage_end, cfg.n_folds)
    paths = enumerate_paths(folds, cfg.k_test_folds)
    if not paths:
        raise ValueError(
            f"CPCV produced 0 paths from n_folds={cfg.n_folds}, "
            f"k_test_folds={cfg.k_test_folds}"
        )

    # Note on purge/embargo: forward-compatible no-op. See module
    # docstring for the why. Compute the deltas here so the values get
    # surfaced in logs/tests once we wire the train side.
    _purge = timedelta(days=cfg.purge_days)
    _embargo = timedelta(days=cfg.embargo_days)
    del _purge, _embargo  # silence unused for now

    for path in paths:
        accumulated: list[Trade] = []
        for start, end in path.test_intervals:
            res = run_backtest(
                spec,
                start=start,
                end=end,
                cost=cost,
                feature_root=feature_root,
                session_calendar=session_calendar,
            )
            accumulated.extend(res.trades)
        path.trades = accumulated
        path.stats = aggregate_trades(accumulated)

    summary = CPCVSummary(
        spec_id=spec.metadata.id,
        spec_version=spec.metadata.version,
        config=cfg,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        folds=folds,
        paths=paths,
    )
    _populate_distribution(summary)
    summary.passed, summary.fail_reason = _evaluate_gate(summary)
    return summary


def _populate_distribution(summary: CPCVSummary) -> None:
    """Compute median, IQR, pct-negative across paths with trades."""
    sharpes = [
        p.stats.sharpe
        for p in summary.paths
        if p.stats is not None and p.stats.total_trades > 0
    ]
    summary.paths_with_trades = len(sharpes)
    if not sharpes:
        return
    sharpes_sorted = sorted(sharpes)
    summary.median_sharpe = statistics.median(sharpes_sorted)
    if len(sharpes_sorted) >= 4:
        # Inclusive quartiles via statistics.quantiles for stability.
        q1, _, q3 = statistics.quantiles(
            sharpes_sorted, n=4, method="inclusive"
        )
        summary.iqr_sharpe = q3 - q1
    else:
        summary.iqr_sharpe = max(sharpes_sorted) - min(sharpes_sorted)
    summary.pct_paths_negative = (
        sum(1 for s in sharpes_sorted if s < 0) / len(sharpes_sorted)
    )


def _evaluate_gate(summary: CPCVSummary) -> tuple[bool, str]:
    """Apply the CPCV gate per doc 05:343 (configurable thresholds).

    Requires (a) at least one path produced trades and (b) median
    Sharpe ≥ ``median_sharpe_threshold`` and (c) pct paths negative ≤
    ``max_pct_paths_negative``.
    """
    cfg = summary.config
    if summary.paths_with_trades == 0:
        return False, "no path produced trades — strategy never fired"
    if summary.median_sharpe < cfg.median_sharpe_threshold:
        return False, (
            f"median Sharpe ({summary.median_sharpe:+.3f}) below "
            f"threshold ({cfg.median_sharpe_threshold:+.3f})"
        )
    if summary.pct_paths_negative > cfg.max_pct_paths_negative:
        return False, (
            f"pct paths negative ({summary.pct_paths_negative:.1%}) "
            f"exceeds max ({cfg.max_pct_paths_negative:.1%})"
        )
    return True, ""
