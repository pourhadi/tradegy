"""Walk-forward validation for the options backtest runner.

Mirror of `tradegy.harness.walk_forward` but adapted to the chain-
snapshot iteration model. The futures harness operates over bar
streams keyed by spec; the options harness operates over chain
snapshots keyed by `(source_id, ticker)`. Otherwise the discipline
is identical: rolling (train, test) windows, in-sample vs out-of-
sample comparison, OOS-within-50%-of-IS gate.

Per `05_backtest_harness.md` and `07_auto_generation.md:171`:
walk-forward exists to detect overfitting and regime fragility.
A single multi-year backtest with positive Sharpe is in-sample
on every bar; only out-of-sample windows test whether the edge
generalizes to data the strategy hasn't been exposed to.

What this module does:

  - Slide rolling (train, test) windows across the chain-snapshot
    coverage span.
  - For each window, run `run_options_backtest_portfolio` (single
    strategy = list of one) twice — train range, test range — using
    the SAME strategy parameters in both halves. No within-window
    parameter optimization (would compound multiple-testing).
  - Aggregate per-window in-sample / OOS Sharpe and trade counts.
  - Apply the OOS Sharpe ≥ 50% × IS Sharpe gate.

Sharpe convention: per-trade dollar Sharpe (mean(pnl) / stdev(pnl))
matches the futures harness's per-trade R-Sharpe semantics. Not
period-annualized; the gate is a relative comparison so absolute
units cancel.

What this module does NOT do (filed as known-deferred):

  - Within-train parameter optimization (compounds multiple-testing
    burden; needs Deflated Sharpe to be meaningful).
  - CPCV — separate module (`tradegy.options.cpcv`) when ported.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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


@dataclass(frozen=True)
class OptionsWalkForwardConfig:
    """Rolling-window configuration for options walk-forward.

    Defaults match the futures convention: 3y train / 1y test / 1y
    roll. For options chain data spanning 6 years this produces 3
    windows (train years 0-3 / test year 3, train 1-4 / test 4,
    train 2-5 / test 5).
    """

    train_years: float = 3.0
    test_years: float = 1.0
    roll_years: float = 1.0

    def __post_init__(self) -> None:
        if (
            self.train_years <= 0
            or self.test_years <= 0
            or self.roll_years <= 0
        ):
            raise ValueError("train/test/roll years must be positive")


@dataclass
class OptionsWalkForwardWindow:
    """One (train, test) split. Both halves carry the full
    `PortfolioBacktestResult` so callers can drill into per-trade
    detail or per-strategy breakdown.
    """

    index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    in_sample: PortfolioBacktestResult | None = None
    out_of_sample: PortfolioBacktestResult | None = None


@dataclass
class OptionsWalkForwardSummary:
    """Aggregated walk-forward metrics + the pass/fail decision."""

    strategy_ids: tuple[str, ...]
    config: OptionsWalkForwardConfig
    coverage_start: datetime
    coverage_end: datetime
    windows: list[OptionsWalkForwardWindow] = field(default_factory=list)
    avg_in_sample_sharpe: float = 0.0
    avg_oos_sharpe: float = 0.0
    worst_window_oos_sharpe: float = 0.0
    avg_in_sample_trades: float = 0.0
    avg_oos_trades: float = 0.0
    passed: bool = False
    fail_reason: str = ""


def split_windows(
    coverage_start: datetime,
    coverage_end: datetime,
    config: OptionsWalkForwardConfig,
) -> list[OptionsWalkForwardWindow]:
    """Produce rolling (train, test) windows over the coverage span.

    Same algorithm as `harness.walk_forward.split_windows`. The
    final window's `test_end` is bounded by `coverage_end`; partial
    test windows that would exceed `coverage_end` are dropped.
    """
    train_delta = timedelta(days=int(round(config.train_years * 365.25)))
    test_delta = timedelta(days=int(round(config.test_years * 365.25)))
    roll_delta = timedelta(days=int(round(config.roll_years * 365.25)))

    windows: list[OptionsWalkForwardWindow] = []
    idx = 0
    train_start = coverage_start
    while True:
        train_end = train_start + train_delta
        test_start = train_end
        test_end = test_start + test_delta
        if test_end > coverage_end:
            break
        windows.append(
            OptionsWalkForwardWindow(
                index=idx,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
            )
        )
        idx += 1
        train_start = train_start + roll_delta
    return windows


def trade_dollar_sharpe(trades: list[ClosedTrade]) -> float:
    """Per-trade dollar Sharpe.

    Matches the futures harness's per-trade Sharpe semantics
    (mean / stdev of P&L distribution). Not period-annualized; the
    walk-forward gate is a relative comparison so absolute units
    cancel.

    Returns 0.0 for fewer than 2 trades or when stdev is 0.
    """
    if len(trades) < 2:
        return 0.0
    pnls = [t.closed_pnl_dollars for t in trades]
    std = statistics.stdev(pnls)
    if std <= 0:
        return 0.0
    return statistics.fmean(pnls) / std


def run_options_walk_forward(
    *,
    strategies: list[OptionStrategy],
    source_id: str,
    ticker: str,
    coverage_start: datetime,
    coverage_end: datetime,
    config: OptionsWalkForwardConfig | None = None,
    cost: OptionCostModel | None = None,
    rules: ManagementRules | None = None,
    rules_by_id: dict[str, ManagementRules] | None = None,
    risk: RiskManager | None = None,
    root: Path | None = None,
) -> OptionsWalkForwardSummary:
    """Run a rolling-window walk-forward and produce the summary.

    `strategies` is a list — single-strategy walk-forward passes a
    single-element list. Multi-strategy (portfolio) walk-forward
    passes the same list that would be passed to
    `run_options_backtest_portfolio`. The OOS gate is applied to
    the AGGREGATE portfolio P&L across all strategies, not per-
    strategy (a portfolio is a single thing being tested).

    For each window, snapshots are loaded fresh from parquet via
    `iter_chain_snapshots(start=..., end=...)`. Loading per-window
    keeps memory bounded and makes each backtest hermetic.
    """
    cfg = config or OptionsWalkForwardConfig()
    cost = cost or OptionCostModel()
    windows = split_windows(coverage_start, coverage_end, cfg)
    if not windows:
        raise ValueError(
            "options walk-forward: coverage span is too short for the "
            "requested train+test windows"
        )

    is_sharpes: list[float] = []
    oos_sharpes: list[float] = []
    is_trades: list[int] = []
    oos_trades: list[int] = []

    for win in windows:
        is_snaps = iter_chain_snapshots(
            source_id, ticker=ticker,
            start=win.train_start, end=win.train_end,
            root=root,
        )
        in_res = run_options_backtest_portfolio(
            strategies=strategies,
            snapshots=is_snaps,
            cost=cost, rules=rules, rules_by_id=rules_by_id, risk=risk,
        )
        oos_snaps = iter_chain_snapshots(
            source_id, ticker=ticker,
            start=win.test_start, end=win.test_end,
            root=root,
        )
        oos_res = run_options_backtest_portfolio(
            strategies=strategies,
            snapshots=oos_snaps,
            cost=cost, rules=rules, rules_by_id=rules_by_id, risk=risk,
        )
        win.in_sample = in_res
        win.out_of_sample = oos_res

        is_sharpes.append(trade_dollar_sharpe(in_res.aggregate_closed_trades))
        oos_sharpes.append(trade_dollar_sharpe(oos_res.aggregate_closed_trades))
        is_trades.append(in_res.n_closed_trades)
        oos_trades.append(oos_res.n_closed_trades)

    summary = OptionsWalkForwardSummary(
        strategy_ids=tuple(s.id for s in strategies),
        config=cfg,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        windows=windows,
    )
    if is_sharpes:
        summary.avg_in_sample_sharpe = statistics.fmean(is_sharpes)
        summary.avg_in_sample_trades = statistics.fmean(is_trades)
    if oos_sharpes:
        summary.avg_oos_sharpe = statistics.fmean(oos_sharpes)
        summary.worst_window_oos_sharpe = min(oos_sharpes)
        summary.avg_oos_trades = statistics.fmean(oos_trades)

    summary.passed, summary.fail_reason = _evaluate_gate(summary)
    return summary


def _evaluate_gate(
    summary: OptionsWalkForwardSummary,
) -> tuple[bool, str]:
    """Apply the OOS-within-50%-of-IS gate per docs/07:171.

    Negative-or-zero IS Sharpe → fail with "no edge to validate"
    (a portfolio that loses in-sample has nothing to validate OOS).
    """
    if not summary.windows:
        return False, "no windows generated"
    if summary.avg_in_sample_sharpe <= 0:
        return False, (
            f"avg in-sample Sharpe ({summary.avg_in_sample_sharpe:.3f}) "
            "is not positive — strategy has no in-sample edge to validate"
        )
    ratio = summary.avg_oos_sharpe / summary.avg_in_sample_sharpe
    if ratio < 0.5:
        return False, (
            f"avg OOS Sharpe ({summary.avg_oos_sharpe:.3f}) is "
            f"< 50% of in-sample ({summary.avg_in_sample_sharpe:.3f}); "
            f"ratio = {ratio:.2f}"
        )
    return True, ""
