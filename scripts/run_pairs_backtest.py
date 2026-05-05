#!/usr/bin/env python3
"""Run a pairs backtest and report stats. CLI driver for
src/tradegy/harness/pairs.py.

Usage:
    uv run python scripts/run_pairs_backtest.py
"""
from __future__ import annotations

import sys

from tradegy.harness.execution import CostModel
from tradegy.harness.pairs import (
    PairsConfig,
    PairSide,
    run_pairs_backtest,
)


def _print_result(label: str, result) -> None:
    print(f"=== {label} ===")
    print(f"  pair_id           {result.config.pair_id}")
    print(f"  side              {result.config.side.value}")
    print(f"  trades            {len(result.trades):,}")
    print(f"  win_rate          {100 * result.win_rate:.1f}%")
    print(f"  total_pnl         ${result.total_pnl:+.2f}")
    print(f"  avg_pnl_per_trade ${result.avg_pnl:+.2f}")
    print(f"  per_trade_sharpe  {result.per_trade_sharpe:+.4f}")
    print(f"  profit_factor     {result.profit_factor:.3f}")
    print(f"  avg_holding_bars  {result.avg_holding_bars:.1f}")
    if result.trades:
        wins = [t.total_pnl for t in result.trades if t.total_pnl > 0]
        losses = [t.total_pnl for t in result.trades if t.total_pnl < 0]
        if wins:
            print(f"  avg_win           ${sum(wins) / len(wins):+.2f}")
        if losses:
            print(f"  avg_loss          ${sum(losses) / len(losses):+.2f}")
    print(f"  coverage          {result.coverage_start} → {result.coverage_end}")
    print()


def main() -> int:
    # Two specs: long-A-short-B and short-A-long-B for the MES-SPY pair.
    # Hedge ratio: 1 MES contract = $5/pt × ~5500 = $27.5K. 50 SPY shares
    # at ~$550 = $27.5K. Approximately dollar-neutral.

    base = dict(
        leg_a_instrument="MES",
        leg_b_instrument="SPY",
        n_a=1,
        n_b=50,
        multiplier_a=5.0,
        multiplier_b=1.0,
        spread_zscore_feature="mes_spy_ratio_zscore_60m",
        entry_threshold=2.0,
        rth_only=True,
        rth_session_feature="mes_xnys_session_position",
        rth_lo=0.05,
        rth_hi=0.90,
        max_attempts_per_session=5,
        cost_per_trade=2.00,  # tight estimate: MES tick + small SPY slip
    )

    print("Running MES-SPY pairs backtest sweep (1m, RTH-only, 7yr)...")
    print()

    # Three exit configurations to test:
    # A. z-score exit (current behavior, exits early on oscillation)
    # B. fixed time-stop only (no z-score-based exit)
    # C. tight time-stop (5min) — capture short-window mean reversion
    sweeps = [
        ("z-exit at 0.5σ, time-stop 30m", dict(use_zscore_exit=True, exit_threshold=0.5, max_holding_bars=30)),
        ("z-exit at 0.0σ (full revert), time-stop 30m", dict(use_zscore_exit=True, exit_threshold=0.0, max_holding_bars=30)),
        ("z-exit DISABLED, time-stop 5m", dict(use_zscore_exit=False, exit_threshold=0.5, max_holding_bars=5)),
        ("z-exit DISABLED, time-stop 15m", dict(use_zscore_exit=False, exit_threshold=0.5, max_holding_bars=15)),
        ("z-exit DISABLED, time-stop 30m", dict(use_zscore_exit=False, exit_threshold=0.5, max_holding_bars=30)),
        ("z-exit DISABLED, time-stop 60m", dict(use_zscore_exit=False, exit_threshold=0.5, max_holding_bars=60)),
    ]

    for label, overrides in sweeps:
        params = {**base, **overrides}
        long_cfg = PairsConfig(
            pair_id=f"mes_spy_long_{label.replace(' ', '_')}",
            side=PairSide.LONG_A_SHORT_B,
            **params,
        )
        short_cfg = PairsConfig(
            pair_id=f"mes_spy_short_{label.replace(' ', '_')}",
            side=PairSide.SHORT_A_LONG_B,
            **params,
        )
        long_r = run_pairs_backtest(long_cfg)
        short_r = run_pairs_backtest(short_cfg)
        combined_n = len(long_r.trades) + len(short_r.trades)
        combined_pnl = long_r.total_pnl + short_r.total_pnl
        avg = combined_pnl / combined_n if combined_n else 0
        # Combined Sharpe: per-trade
        all_pnls = [t.total_pnl for t in long_r.trades] + [t.total_pnl for t in short_r.trades]
        if len(all_pnls) >= 2:
            import statistics
            sharpe = statistics.mean(all_pnls) / statistics.pstdev(all_pnls) if statistics.pstdev(all_pnls) > 0 else 0.0
        else:
            sharpe = 0.0
        wins = sum(1 for p in all_pnls if p > 0)
        win_rate = wins / combined_n if combined_n else 0
        print(f"=== {label} ===")
        print(f"  trades            {combined_n:,}  (long {len(long_r.trades):,}, short {len(short_r.trades):,})")
        print(f"  combined_pnl      ${combined_pnl:+,.2f}")
        print(f"  avg_pnl_per_trade ${avg:+.2f}")
        print(f"  win_rate          {100*win_rate:.1f}%")
        print(f"  per_trade_sharpe  {sharpe:+.4f}")
        print(f"  avg_holding (long/short) {long_r.avg_holding_bars:.1f} / {short_r.avg_holding_bars:.1f}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
