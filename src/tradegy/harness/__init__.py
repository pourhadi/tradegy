"""Backtest harness MVP — single spec, single window.

Per 05_backtest_harness.md. Modes implemented: ``single``,
``walk_forward``, ``cpcv``. Modes deferred: ``sensitivity``,
``variant_sweep``, ``regression``, ``batch``.
"""
from __future__ import annotations

from tradegy.harness.cpcv import (  # noqa: F401
    CPCVConfig,
    CPCVPath,
    CPCVSummary,
    FoldBoundary,
    enumerate_paths,
    run_cpcv,
    split_folds,
)
from tradegy.harness.execution import CostModel  # noqa: F401
from tradegy.harness.runner import BacktestResult, run_backtest  # noqa: F401
from tradegy.harness.stats import AggregateStats, aggregate_trades  # noqa: F401
from tradegy.harness.walk_forward import (  # noqa: F401
    WalkForwardConfig,
    WalkForwardSummary,
    WalkForwardWindow,
    run_walk_forward,
    split_windows,
)
