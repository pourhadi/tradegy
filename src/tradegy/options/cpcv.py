"""Combinatorial Purged Cross-Validation for the options runner.

Mirror of `tradegy.harness.cpcv` adapted to chain-snapshot iteration.
Per `05_backtest_harness.md:334-343` and the CLAUDE.md gate convention:

  - Divide coverage into N folds (default 10).
  - Generate C(N, k) paths by choosing k test folds per path
    (default k = 2 → 45 paths from N = 10).
  - Run the strategy on each path's union of test folds; concatenate
    closed trades; compute per-path Sharpe.
  - Apply the gate: median Sharpe > 0.8 AND pct paths negative < 20%.

Per-trade dollar Sharpe convention matches the walk-forward module
(mean / stdev of closed_pnl_dollars distribution per path). Not
period-annualized; the gate's threshold (0.8) was tuned for the
futures harness's per-trade R-Sharpe — for options the threshold
should be re-validated against established options strategies before
being trusted as a hard pass/fail. Documented as a known gap.

Purge / embargo: forward-compatible no-ops, same as the futures
module. Activates the moment a fitting step is added to the train
side. We currently evaluate frozen parameters across folds.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from pathlib import Path

from tradegy.options.chain_io import iter_chain_snapshots
from tradegy.options.cost_model import OptionCostModel
from tradegy.options.portfolio_runner import (
    PortfolioBacktestResult,
    run_options_backtest_portfolio,
)
from tradegy.options.risk import RiskManager
from tradegy.options.runner import ClosedTrade
from tradegy.options.strategy import ManagementRules, OptionStrategy
from tradegy.options.walk_forward import trade_dollar_sharpe


@dataclass(frozen=True)
class OptionsCPCVConfig:
    """CPCV configuration. Defaults match doc 05:336/343."""

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
class OptionsFoldBoundary:
    index: int
    start: datetime
    end: datetime


@dataclass
class OptionsCPCVPath:
    """One combinatorial path. `trades` is the concatenation across
    fold backtests, used to compute path-level Sharpe.
    """

    index: int
    test_fold_indices: tuple[int, ...]
    test_intervals: list[tuple[datetime, datetime]]
    trades: list[ClosedTrade] = field(default_factory=list)
    sharpe: float = 0.0


@dataclass
class OptionsCPCVSummary:
    strategy_ids: tuple[str, ...]
    config: OptionsCPCVConfig
    coverage_start: datetime
    coverage_end: datetime
    folds: list[OptionsFoldBoundary] = field(default_factory=list)
    paths: list[OptionsCPCVPath] = field(default_factory=list)
    paths_with_trades: int = 0
    median_sharpe: float = 0.0
    iqr_sharpe: float = 0.0
    pct_paths_negative: float = 0.0
    passed: bool = False
    fail_reason: str = ""


def split_folds(
    coverage_start: datetime,
    coverage_end: datetime,
    n_folds: int,
) -> list[OptionsFoldBoundary]:
    """Cut [coverage_start, coverage_end) into n_folds equal-width
    folds. Last fold absorbs any rounding remainder so it always
    reaches coverage_end. Identical algorithm to harness.cpcv.
    """
    if coverage_end <= coverage_start:
        raise ValueError("coverage_end must be after coverage_start")
    span = coverage_end - coverage_start
    width = span / n_folds
    folds: list[OptionsFoldBoundary] = []
    for i in range(n_folds):
        start = coverage_start + width * i
        end = (
            coverage_start + width * (i + 1)
            if i < n_folds - 1 else coverage_end
        )
        folds.append(OptionsFoldBoundary(index=i, start=start, end=end))
    return folds


def enumerate_paths(
    folds: list[OptionsFoldBoundary], k_test_folds: int,
) -> list[OptionsCPCVPath]:
    paths: list[OptionsCPCVPath] = []
    for idx, combo in enumerate(combinations(range(len(folds)), k_test_folds)):
        intervals = [(folds[i].start, folds[i].end) for i in combo]
        paths.append(OptionsCPCVPath(
            index=idx,
            test_fold_indices=combo,
            test_intervals=intervals,
        ))
    return paths


def run_options_cpcv(
    *,
    strategies: list[OptionStrategy],
    source_id: str,
    ticker: str,
    coverage_start: datetime,
    coverage_end: datetime,
    config: OptionsCPCVConfig | None = None,
    cost: OptionCostModel | None = None,
    rules: ManagementRules | None = None,
    rules_by_id: dict[str, ManagementRules] | None = None,
    risk: RiskManager | None = None,
    root: Path | None = None,
) -> OptionsCPCVSummary:
    """Run combinatorial purged CV over the coverage span, return
    summary with the gate applied.

    Per-path procedure:

      1. For each test fold in the path, load chain snapshots for
         that fold's window and run `run_options_backtest_portfolio`.
      2. Concatenate closed trades across folds.
      3. Compute per-trade dollar Sharpe on the concatenation.

    Cross-path: collect Sharpes from paths with ≥ 1 trade, compute
    median / IQR / pct-negative, apply the gate.
    """
    cfg = config or OptionsCPCVConfig()
    cost = cost or OptionCostModel()
    folds = split_folds(coverage_start, coverage_end, cfg.n_folds)
    paths = enumerate_paths(folds, cfg.k_test_folds)
    if not paths:
        raise ValueError(
            f"CPCV produced 0 paths from n_folds={cfg.n_folds}, "
            f"k_test_folds={cfg.k_test_folds}"
        )

    # Cache fold-level results so we don't re-backtest the same fold
    # for every path that includes it. C(10, 2) = 45 paths but only
    # 10 unique folds — 4.5x speedup.
    fold_trades_cache: dict[int, list[ClosedTrade]] = {}

    def _trades_for_fold(fold_idx: int) -> list[ClosedTrade]:
        if fold_idx in fold_trades_cache:
            return fold_trades_cache[fold_idx]
        fold = folds[fold_idx]
        snaps = iter_chain_snapshots(
            source_id, ticker=ticker,
            start=fold.start, end=fold.end,
            root=root,
        )
        res: PortfolioBacktestResult = run_options_backtest_portfolio(
            strategies=strategies,
            snapshots=snaps,
            cost=cost, rules=rules, rules_by_id=rules_by_id, risk=risk,
        )
        fold_trades_cache[fold_idx] = list(res.aggregate_closed_trades)
        return fold_trades_cache[fold_idx]

    for path in paths:
        accumulated: list[ClosedTrade] = []
        for fold_idx in path.test_fold_indices:
            accumulated.extend(_trades_for_fold(fold_idx))
        path.trades = accumulated
        path.sharpe = trade_dollar_sharpe(accumulated)

    summary = OptionsCPCVSummary(
        strategy_ids=tuple(s.id for s in strategies),
        config=cfg,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        folds=folds,
        paths=paths,
    )
    _populate_distribution(summary)
    summary.passed, summary.fail_reason = _evaluate_gate(summary)
    return summary


def _populate_distribution(summary: OptionsCPCVSummary) -> None:
    sharpes = [p.sharpe for p in summary.paths if len(p.trades) > 0]
    summary.paths_with_trades = len(sharpes)
    if not sharpes:
        return
    sharpes_sorted = sorted(sharpes)
    summary.median_sharpe = statistics.median(sharpes_sorted)
    if len(sharpes_sorted) >= 4:
        q1, _, q3 = statistics.quantiles(
            sharpes_sorted, n=4, method="inclusive",
        )
        summary.iqr_sharpe = q3 - q1
    else:
        summary.iqr_sharpe = max(sharpes_sorted) - min(sharpes_sorted)
    summary.pct_paths_negative = (
        sum(1 for s in sharpes_sorted if s < 0) / len(sharpes_sorted)
    )


def _evaluate_gate(
    summary: OptionsCPCVSummary,
) -> tuple[bool, str]:
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
