"""Aggregate trade statistics for backtest reports.

Per 05_backtest_harness.md:202-221, the per-spec aggregate block
contains expectancy, sharpe, drawdown, win rate, etc. The MVP computes
the bedrock subset; regime-stratified, parameter-sensitivity, and
baseline-comparison blocks land in subsequent slices.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from tradegy.strategies.types import Trade


@dataclass
class AggregateStats:
    total_trades: int
    expectancy_R: float
    total_pnl: float
    total_pnl_R: float
    win_rate: float
    avg_win_R: float
    avg_loss_R: float
    profit_factor: float
    avg_holding_bars: float
    sharpe: float
    max_drawdown: float


def aggregate_trades(trades: list[Trade]) -> AggregateStats:
    """Compute the MVP stats block for a list of closed trades."""
    if not trades:
        return AggregateStats(
            total_trades=0,
            expectancy_R=0.0,
            total_pnl=0.0,
            total_pnl_R=0.0,
            win_rate=0.0,
            avg_win_R=0.0,
            avg_loss_R=0.0,
            profit_factor=0.0,
            avg_holding_bars=0.0,
            sharpe=0.0,
            max_drawdown=0.0,
        )

    pnls = [t.net_pnl for t in trades]
    pnls_R = [t.net_pnl_R for t in trades]
    wins_R = [r for r in pnls_R if r > 0]
    losses_R = [r for r in pnls_R if r <= 0]

    win_rate = len(wins_R) / len(trades)
    avg_win_R = statistics.fmean(wins_R) if wins_R else 0.0
    avg_loss_R = statistics.fmean(losses_R) if losses_R else 0.0

    gross_win = sum(t.net_pnl for t in trades if t.net_pnl > 0)
    gross_loss = -sum(t.net_pnl for t in trades if t.net_pnl < 0)
    profit_factor = gross_win / gross_loss if gross_loss > 0 else math.inf

    avg_holding_bars = statistics.fmean(t.holding_bars for t in trades)

    # Per-trade Sharpe (no period normalization — closer to information
    # ratio of trade-level returns). Real harness will compute time-
    # series Sharpe; the MVP stat is honest about its scope.
    if len(pnls_R) > 1:
        mean_R = statistics.fmean(pnls_R)
        std_R = statistics.stdev(pnls_R)
        sharpe = mean_R / std_R if std_R > 0 else 0.0
    else:
        sharpe = 0.0

    # Cumulative-PnL drawdown.
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        cum += pnl
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    return AggregateStats(
        total_trades=len(trades),
        expectancy_R=statistics.fmean(pnls_R),
        total_pnl=sum(pnls),
        total_pnl_R=sum(pnls_R),
        win_rate=win_rate,
        avg_win_R=avg_win_R,
        avg_loss_R=avg_loss_R,
        profit_factor=profit_factor,
        avg_holding_bars=avg_holding_bars,
        sharpe=sharpe,
        max_drawdown=max_dd,
    )
