"""Quick experiment: SPY + IWM + DIA shared-capital portfolio @ $5K.

Uses the new multi_source_runner. Each underlying gets its own
PCS+IC+JL strategies wrapped with IV<0.25; all three streams share
a single $5K RiskManager.

Compare to:
  SPY solo @ $5K = ~28% AnnRoC OOS, worst-window -0.212
  IWM solo @ $5K = +0.080 OOS, worst-window -0.150 (fail gate)
  DIA solo @ $5K = -0.136 OOS, worst-window -0.587 (fail gate)

Question: does shared-capital diversification rescue the gate?
"""
from __future__ import annotations

from datetime import datetime, timedelta

from tradegy.options.cost_model import OptionCostModel
from tradegy.options.multi_source_runner import (
    build_iv_gated_strategies_per_source,
    run_options_backtest_multi_source,
)
from tradegy.options.risk import RiskConfig, RiskManager
from tradegy.options.strategies import (
    IronCondor45dteD16,
    JadeLizard45dte,
    PutCreditSpread45dteD30,
)
from tradegy.options.strategy import ManagementRules


def run_combo(*, underlyings: list[str], capital: float = 5_000.0,
              iv_gate_max: float = 0.25) -> None:
    """Run one underlying-combo backtest and print results."""
    sources = build_iv_gated_strategies_per_source(
        underlyings=underlyings,
        base_strategy_factories=[
            PutCreditSpread45dteD30,
            IronCondor45dteD16,
            JadeLizard45dte,
        ],
        iv_gate_max=iv_gate_max,
        iv_gate_window_days=252,
    )
    risk = RiskManager(RiskConfig(declared_capital=capital))
    rules = ManagementRules(profit_take_pct=0.50, loss_stop_pct=2.0, dte_close=21)

    label = "+".join(underlyings)
    print(f"\n=== {label} @ ${capital:,.0f}, IV<{iv_gate_max} ===")
    result = run_options_backtest_multi_source(
        sources=sources,
        coverage_start=datetime(2020, 1, 1),
        coverage_end=datetime(2026, 5, 2),
        cost=OptionCostModel(),
        rules=rules,
        risk=risk,
    )

    print(f"trades: {result.n_closed_trades}  "
          f"P&L: ${result.realized_pnl_dollars:+,.0f}  "
          f"hit: {result.hit_rate:.1%}  "
          f"rejections: {len(result.rejected_orders)}")
    for source_id, src_result in result.per_source.items():
        n = len(src_result.closed_trades)
        pnl = sum(t.closed_pnl_dollars for t in src_result.closed_trades)
        wins = sum(1 for t in src_result.closed_trades if t.closed_pnl_dollars > 0)
        hr = wins / n if n > 0 else 0.0
        print(f"  {source_id}: {n} trades, ${pnl:+,.0f}, hit {hr:.1%}")


def run_walk_forward(*, underlyings: list[str], capital: float = 5_000.0,
                     iv_gate_max: float = 0.25) -> None:
    from tradegy.options.multi_source_runner import (
        run_multi_source_walk_forward,
    )
    sources = build_iv_gated_strategies_per_source(
        underlyings=underlyings,
        base_strategy_factories=[
            PutCreditSpread45dteD30,
            IronCondor45dteD16,
            JadeLizard45dte,
        ],
        iv_gate_max=iv_gate_max,
    )
    risk = RiskManager(RiskConfig(declared_capital=capital))
    rules = ManagementRules(profit_take_pct=0.50, loss_stop_pct=2.0, dte_close=21)
    label = "+".join(underlyings)
    print(f"\n=== WALK-FWD: {label} @ ${capital:,.0f}, IV<{iv_gate_max} ===")
    summary = run_multi_source_walk_forward(
        sources=sources,
        coverage_start=datetime(2020, 1, 1),
        coverage_end=datetime(2026, 5, 2),
        train_years=3.0, test_years=1.0, roll_years=1.0,
        cost=OptionCostModel(), rules=rules, risk=risk,
    )
    print(f"avg IS Sharpe:  {summary.avg_in_sample_sharpe:+.3f}")
    print(f"avg OOS Sharpe: {summary.avg_oos_sharpe:+.3f}")
    print(f"worst OOS Sharpe: {summary.worst_window_oos_sharpe:+.3f}")
    print(f"avg IS trades:  {summary.avg_in_sample_trades:.1f}")
    print(f"avg OOS trades: {summary.avg_oos_trades:.1f}")
    print(f"GATE: {'✅ PASS' if summary.passed else '❌ FAIL'}"
          f"{'' if summary.passed else ' — ' + summary.fail_reason}")
    for w in summary.windows:
        is_pnl = (w.in_sample.realized_pnl_dollars
                  if w.in_sample else 0)
        oos_pnl = (w.out_of_sample.realized_pnl_dollars
                   if w.out_of_sample else 0)
        print(
            f"  win {w.index}: train {w.train_start.date()}→{w.train_end.date()} "
            f"({w.in_sample.n_closed_trades} trades, ${is_pnl:+,.0f})  "
            f"test {w.test_start.date()}→{w.test_end.date()} "
            f"({w.out_of_sample.n_closed_trades} trades, ${oos_pnl:+,.0f})"
        )


def run_multi_dte_walk_forward(
    *, underlyings: list[str], capital: float = 5_000.0,
    iv_gate_max: float = 0.25,
) -> None:
    """Stack 30 DTE + 45 DTE variants of the validated PCS+IC+JL
    portfolio on each underlying. Faster cycling on the 30-DTE
    side should multiply trade count.
    """
    from tradegy.options.multi_source_runner import (
        SourceSpec,
        build_iv_gated_strategies_per_source,
        run_multi_source_walk_forward,
    )
    from tradegy.options.registry import get_strategy

    # Build sources manually so we can inject 30 DTE + 45 DTE
    # together. build_iv_gated_strategies_per_source takes class
    # constructors; we use it for the 45 DTE leg, then mutate to
    # add 30 DTE wrapped strategies.
    sources_45 = build_iv_gated_strategies_per_source(
        underlyings=underlyings,
        base_strategy_factories=[
            PutCreditSpread45dteD30,
            IronCondor45dteD16,
            JadeLizard45dte,
        ],
        iv_gate_max=iv_gate_max,
    )
    # Add 30-DTE wrapped variants per source.
    from dataclasses import replace
    from tradegy.options.strategies import IvGatedStrategy
    sources: list[SourceSpec] = []
    for spec in sources_45:
        ticker = spec.ticker
        new_strats = list(spec.strategies)
        for sid in (
            "put_credit_spread_30dte_d30",
            "iron_condor_30dte_d16",
            "jade_lizard_30dte",
        ):
            base_30 = get_strategy(sid)
            wrapped = IvGatedStrategy(
                base=base_30, max_iv_rank=iv_gate_max, window_days=252,
            )
            wrapped = replace(wrapped, id=f"{wrapped.id}_{ticker.lower()}")
            new_strats.append(wrapped)
        sources.append(SourceSpec(
            source_id=spec.source_id, ticker=ticker,
            strategies=tuple(new_strats),
        ))

    risk = RiskManager(RiskConfig(declared_capital=capital))
    rules = ManagementRules(profit_take_pct=0.50, loss_stop_pct=2.0, dte_close=21)
    label = "+".join(underlyings)
    print(f"\n=== MULTI-DTE WALK-FWD: {label} (30+45 DTE) @ ${capital:,.0f}, IV<{iv_gate_max} ===")
    summary = run_multi_source_walk_forward(
        sources=sources,
        coverage_start=datetime(2020, 1, 1),
        coverage_end=datetime(2026, 5, 2),
        train_years=3.0, test_years=1.0, roll_years=1.0,
        cost=OptionCostModel(), rules=rules, risk=risk,
    )
    print(f"avg IS Sharpe:    {summary.avg_in_sample_sharpe:+.3f}")
    print(f"avg OOS Sharpe:   {summary.avg_oos_sharpe:+.3f}")
    print(f"worst OOS Sharpe: {summary.worst_window_oos_sharpe:+.3f}")
    print(f"avg IS trades:    {summary.avg_in_sample_trades:.1f}")
    print(f"avg OOS trades:   {summary.avg_oos_trades:.1f}")
    print(f"GATE: {'✅ PASS' if summary.passed else '❌ FAIL'}"
          f"{'' if summary.passed else ' — ' + summary.fail_reason}")
    for w in summary.windows:
        is_pnl = w.in_sample.realized_pnl_dollars if w.in_sample else 0
        oos_pnl = w.out_of_sample.realized_pnl_dollars if w.out_of_sample else 0
        print(
            f"  win {w.index}: IS={w.in_sample.n_closed_trades} trades "
            f"${is_pnl:+,.0f} → OOS={w.out_of_sample.n_closed_trades} trades "
            f"${oos_pnl:+,.0f}"
        )


def main() -> None:
    # 7-underlying multi-source experiment with sector/asset ETFs
    # added (GLD, TLT, XLE, EEM) for IV-regime diversification.
    # Run in-sample combos first to find the best mix, then walk-
    # forward the most promising.

    # Each new ETF solo @ $5K to baseline.
    for u in ["GLD", "TLT", "XLE", "EEM"]:
        run_combo(underlyings=[u])

    # Equity-only baseline for comparison (already known to pass).
    run_combo(underlyings=["SPY", "IWM", "QQQ"])

    # Add each diversifier one at a time to SPY+IWM+QQQ.
    run_combo(underlyings=["SPY", "IWM", "QQQ", "GLD"])
    run_combo(underlyings=["SPY", "IWM", "QQQ", "TLT"])
    run_combo(underlyings=["SPY", "IWM", "QQQ", "XLE"])
    run_combo(underlyings=["SPY", "IWM", "QQQ", "EEM"])

    # All 7 — maximum diversification.
    run_combo(underlyings=["SPY", "IWM", "QQQ", "GLD", "TLT", "XLE", "EEM"])

    # Walk-forward the most promising combos found in the
    # in-sample sweep: EEM solo (best single), and SPY+IWM+QQQ+EEM
    # (best multi).
    run_walk_forward(underlyings=["EEM"])
    run_walk_forward(underlyings=["SPY", "IWM", "QQQ", "EEM"])


if __name__ == "__main__":
    main()
