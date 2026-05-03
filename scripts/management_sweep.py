#!/usr/bin/env python3
"""Multi-year backtest of [PCS, IC, JL] × management-rule grid.

Per the Phase D-4 finding (2026-05-03): our PCS bare result is +3%
annualized vs practitioner-canon 8-15%. The default ManagementRules
(50% profit / 21 DTE / 200% loss) might not be optimal; tastytrade
research often cites tighter profit targets (25%) and shorter DTE
(14) as superior. This sweep tests the grid on the full 6-year SPX
window and surfaces which combination produces the best risk-
adjusted return.

Run: python scripts/management_sweep.py
"""
from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Iterable

from tradegy.options.chain_io import iter_chain_snapshots
from tradegy.options.runner import run_options_backtest
from tradegy.options.strategies import (
    IronCondor45dteD16,
    JadeLizard45dte,
    PutCreditSpread45dteD30,
)
from tradegy.options.risk import RiskManager, RiskConfig
from tradegy.options.strategy import ManagementRules


_CAPITAL = 250_000.0
_RISK_CFG = RiskConfig(
    declared_capital=_CAPITAL,
    max_capital_at_risk_pct=0.50,
    max_per_expiration_pct=0.50,
)


def _annualized_sharpe(trades, n_years: float) -> float:
    if len(trades) < 2:
        return float("nan")
    pnls = [t.closed_pnl_dollars for t in trades]
    mean = sum(pnls) / len(pnls)
    if all(p == mean for p in pnls):
        return float("nan")
    var = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
    sd = math.sqrt(var)
    if sd <= 0:
        return float("nan")
    trades_per_year = len(pnls) / n_years
    return mean / sd * math.sqrt(trades_per_year)


def main() -> None:
    print("Loading 6-year SPX chain...")
    t0 = time.time()
    snaps = list(iter_chain_snapshots("spx_options_chain", ticker="SPX"))
    print(f"  {len(snaps)} snaps in {time.time()-t0:.0f}s")
    n_years = (snaps[-1].ts_utc.date() - snaps[0].ts_utc.date()).days / 365.25
    print(f"  span: {n_years:.2f} years\n")

    rule_grid: list[tuple[str, ManagementRules]] = [
        ("default 50/21/200", ManagementRules()),
        ("tighter 25/21/200", ManagementRules(profit_take_pct=0.25)),
        ("more-time 50/14/200", ManagementRules(dte_close=14)),
        ("aggressive 25/14/300", ManagementRules(
            profit_take_pct=0.25, dte_close=14, loss_stop_pct=3.0,
        )),
        ("very-tight 25/21/100", ManagementRules(
            profit_take_pct=0.25, loss_stop_pct=1.0,
        )),
        ("loose 75/21/200", ManagementRules(profit_take_pct=0.75)),
    ]
    strategy_grid = [
        ("PCS", PutCreditSpread45dteD30()),
        ("IC", IronCondor45dteD16()),
        ("JL", JadeLizard45dte()),
    ]
    risk = RiskManager(_RISK_CFG)

    header = (
        f"{'Strategy':<5s} {'Rules':<24s} "
        f"{'Trades':>6s} {'Hit%':>5s} {'P&L':>10s} {'MaxDD':>9s} "
        f"{'Sharpe':>7s} {'RoC%':>6s} {'AnnRoC%':>8s}"
    )
    print(header)
    print("-" * len(header))
    for s_label, strat in strategy_grid:
        for r_label, rules in rule_grid:
            tr0 = time.time()
            r = run_options_backtest(
                strategy=strat, snapshots=snaps, risk=risk, rules=rules,
            )
            if not r.n_closed_trades:
                print(f"  {s_label:<5s} {r_label:<24s} no closed trades")
                continue
            roc = r.realized_pnl_dollars / _CAPITAL * 100
            ann_roc = roc / n_years
            sh = _annualized_sharpe(r.closed_trades, n_years)
            print(
                f"  {s_label:<5s} {r_label:<24s} "
                f"{r.n_closed_trades:>4d}  {r.hit_rate*100:>4.0f}  "
                f"${r.realized_pnl_dollars:>+8,.0f}  "
                f"${r.max_drawdown_dollars:>+7,.0f}  "
                f"{sh:>+6.2f}  {roc:>+5.1f}%  {ann_roc:>+6.1f}%"
            )
        print()


if __name__ == "__main__":
    main()
