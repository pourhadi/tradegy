"""Walk-forward validation for the backtest harness.

Per 05_backtest_harness.md:274-278 walk-forward is a sequence of
rolling (train, test) windows. For each window the strategy is
evaluated in-sample over the train range and then evaluated out-of-
sample over the test range with the SAME parameters. The aggregate
report compares in-sample vs out-of-sample to detect overfitting and
regime fragility.

What this MVP does:
  - Slide rolling (train, test) windows across the data with
    configurable train/test/roll durations.
  - For each window, call run_backtest twice (train range, test range)
    using the spec's current parameters unchanged.
  - Aggregate per-window in-sample / OOS Sharpe and trade counts.
  - Report the gate per docs/07_auto_generation.md:171 ("Out-of-sample
    Sharpe within 50% of in-sample Sharpe").

What this MVP does NOT do (filed as known-deferred):
  - Parameter optimization within the train window (within-envelope
    grid search). Would compound the multiple-testing problem and
    needs Deflated Sharpe correction to be meaningful.
  - Combinatorial Purged Cross-Validation (CPCV) — different fold
    generation entirely, with purging + embargo. Phase 5+.
  - Deflated Sharpe Ratio — auto-generation prerequisite, not strictly
    needed for single-spec walk-forward.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from tradegy.harness.execution import CostModel
from tradegy.harness.runner import BacktestResult, run_backtest
from tradegy.harness.stats import AggregateStats
from tradegy.specs.schema import StrategySpec


@dataclass(frozen=True)
class WalkForwardConfig:
    """Rolling-window configuration for walk-forward validation.

    All durations in years (fractional allowed). Defaults match the doc's
    suggested 3y train / 1y test / 1y roll.
    """

    train_years: float = 3.0
    test_years: float = 1.0
    roll_years: float = 1.0

    def __post_init__(self) -> None:
        if self.train_years <= 0 or self.test_years <= 0 or self.roll_years <= 0:
            raise ValueError("train/test/roll years must be positive")


@dataclass
class WalkForwardWindow:
    """One (train, test) split. ``in_sample`` and ``out_of_sample`` carry
    the full BacktestResult so callers can drill into per-trade detail."""

    index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    in_sample: BacktestResult | None = None
    out_of_sample: BacktestResult | None = None


@dataclass
class WalkForwardSummary:
    spec_id: str
    spec_version: str
    config: WalkForwardConfig
    coverage_start: datetime
    coverage_end: datetime
    windows: list[WalkForwardWindow] = field(default_factory=list)
    # Aggregates over the OOS halves only.
    avg_in_sample_sharpe: float = 0.0
    avg_oos_sharpe: float = 0.0
    worst_window_oos_sharpe: float = 0.0
    avg_oos_trades: float = 0.0
    avg_in_sample_trades: float = 0.0
    # Gate: OOS Sharpe should be within 50% of in-sample (per
    # 07_auto_generation.md:171).
    passed: bool = False
    fail_reason: str = ""


def split_windows(
    coverage_start: datetime,
    coverage_end: datetime,
    config: WalkForwardConfig,
) -> list[WalkForwardWindow]:
    """Produce rolling (train, test) windows over the coverage span.

    Window 0:
        train: [coverage_start, coverage_start + train_years)
        test:  [train_end,       train_end       + test_years)
    Window N:
        train_start = coverage_start + N * roll_years
        ... etc, until test_end exceeds coverage_end.
    """
    train_delta = timedelta(days=int(round(config.train_years * 365.25)))
    test_delta = timedelta(days=int(round(config.test_years * 365.25)))
    roll_delta = timedelta(days=int(round(config.roll_years * 365.25)))

    windows: list[WalkForwardWindow] = []
    idx = 0
    train_start = coverage_start
    while True:
        train_end = train_start + train_delta
        test_start = train_end
        test_end = test_start + test_delta
        if test_end > coverage_end:
            break
        windows.append(
            WalkForwardWindow(
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


def run_walk_forward(
    spec: StrategySpec,
    *,
    coverage_start: datetime,
    coverage_end: datetime,
    config: WalkForwardConfig | None = None,
    cost: CostModel | None = None,
    feature_root: Path | None = None,
    session_calendar: str = "CMES",
) -> WalkForwardSummary:
    """Run a rolling-window walk-forward and produce the summary.

    Each window's in_sample and out_of_sample BacktestResult are
    populated; the summary aggregates Sharpe + trade counts and applies
    the OOS-within-50%-of-in-sample gate.
    """
    cfg = config or WalkForwardConfig()
    cost = cost or CostModel()
    windows = split_windows(coverage_start, coverage_end, cfg)
    if not windows:
        raise ValueError(
            "walk-forward: coverage span is too short for the requested "
            "train+test windows"
        )

    in_sample_sharpes: list[float] = []
    oos_sharpes: list[float] = []
    in_sample_trades: list[int] = []
    oos_trades: list[int] = []

    for win in windows:
        in_res = run_backtest(
            spec,
            start=win.train_start,
            end=win.train_end,
            cost=cost,
            feature_root=feature_root,
            session_calendar=session_calendar,
        )
        oos_res = run_backtest(
            spec,
            start=win.test_start,
            end=win.test_end,
            cost=cost,
            feature_root=feature_root,
            session_calendar=session_calendar,
        )
        win.in_sample = in_res
        win.out_of_sample = oos_res
        if in_res.stats is not None:
            in_sample_sharpes.append(in_res.stats.sharpe)
            in_sample_trades.append(in_res.stats.total_trades)
        if oos_res.stats is not None:
            oos_sharpes.append(oos_res.stats.sharpe)
            oos_trades.append(oos_res.stats.total_trades)

    summary = WalkForwardSummary(
        spec_id=spec.metadata.id,
        spec_version=spec.metadata.version,
        config=cfg,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        windows=windows,
    )
    if in_sample_sharpes:
        summary.avg_in_sample_sharpe = sum(in_sample_sharpes) / len(in_sample_sharpes)
        summary.avg_in_sample_trades = sum(in_sample_trades) / len(in_sample_trades)
    if oos_sharpes:
        summary.avg_oos_sharpe = sum(oos_sharpes) / len(oos_sharpes)
        summary.worst_window_oos_sharpe = min(oos_sharpes)
        summary.avg_oos_trades = sum(oos_trades) / len(oos_trades)

    summary.passed, summary.fail_reason = _evaluate_gate(summary)
    return summary


def _evaluate_gate(summary: WalkForwardSummary) -> tuple[bool, str]:
    """Apply the OOS-within-50%-of-in-sample gate per docs/07:171.

    A negative-or-zero in-sample Sharpe is treated as "no edge to validate"
    rather than a pass; the gate requires a positive in-sample Sharpe AND
    OOS Sharpe within 50% of it.
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
