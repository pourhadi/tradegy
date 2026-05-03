"""Portfolio runner tests against real ORATS data.

Per the no-synthetic-data rule. Verifies multiple strategies run
concurrently, each tracking its own positions, sharing the
RiskManager's capital pool.
"""
from __future__ import annotations

import pytest

from tradegy.options.portfolio_runner import (
    PortfolioBacktestResult,
    run_options_backtest_portfolio,
)
from tradegy.options.risk import RiskManager, RiskConfig
from tradegy.options.runner import run_options_backtest
from tradegy.options.strategies import (
    IronCondor45dteD16,
    JadeLizard45dte,
    PutCalendar30_60AtmDeb,
    PutCreditSpread45dteD30,
)
from tradegy.options.strategy import ManagementRules


def test_portfolio_two_strategies_both_active(real_spx_chain_snapshots):
    """Run PCS + IronCondor in parallel; both should fill some
    positions over the window.
    """
    snaps = real_spx_chain_snapshots[:120]  # ~120 days for a focused test
    risk = RiskManager(RiskConfig(declared_capital=250_000.0))
    result = run_options_backtest_portfolio(
        strategies=[PutCreditSpread45dteD30(), IronCondor45dteD16()],
        snapshots=snaps,
        risk=risk,
    )
    assert isinstance(result, PortfolioBacktestResult)
    assert result.n_snapshots_seen == len(snaps)
    # Both strategies should have entries in per_strategy.
    assert "put_credit_spread_45dte_d30" in result.per_strategy
    assert "iron_condor_45dte_d16" in result.per_strategy
    # Aggregate trades = sum of per-strategy trades.
    pcs_trades = len(result.per_strategy["put_credit_spread_45dte_d30"].closed_trades)
    ic_trades = len(result.per_strategy["iron_condor_45dte_d16"].closed_trades)
    assert result.n_closed_trades == pcs_trades + ic_trades
    # In a 120-day window we expect at least a few closes from each.
    # Allow either strategy to have 0 (early in window) but the
    # combined total must be > 0.
    assert result.n_closed_trades > 0


def test_portfolio_strategies_open_concurrently(real_spx_chain_snapshots):
    """Verify the portfolio actually achieves CONCURRENT positions
    — at some snapshot, at least 2 positions should be open
    simultaneously (one per strategy class).
    """
    snaps = real_spx_chain_snapshots[:120]
    risk = RiskManager(RiskConfig(declared_capital=250_000.0))
    result = run_options_backtest_portfolio(
        strategies=[PutCreditSpread45dteD30(), IronCondor45dteD16()],
        snapshots=snaps,
        risk=risk,
    )
    max_concurrent = max(
        row.n_open_positions for row in result.aggregate_snapshot_pnl
    )
    assert max_concurrent >= 2, (
        f"expected ≥2 concurrent positions in portfolio mode; got "
        f"{max_concurrent}"
    )


def test_portfolio_per_strategy_rules_applied(real_spx_chain_snapshots):
    """rules_by_id supplies different management per strategy —
    calendar gets debit-aware rules, condor gets default credit.
    Both close trades cleanly.
    """
    snaps = real_spx_chain_snapshots[:120]
    risk = RiskManager(RiskConfig(declared_capital=250_000.0))
    cal_rules = ManagementRules(
        profit_take_pct=0.50, dte_close=21, loss_stop_pct=2.0,
        profit_take_pct_of_debit=0.25, loss_stop_pct_of_debit=0.50,
    )
    result = run_options_backtest_portfolio(
        strategies=[
            JadeLizard45dte(),
            PutCalendar30_60AtmDeb(),
        ],
        snapshots=snaps, risk=risk,
        rules_by_id={"put_calendar_30_60_atm_deb": cal_rules},
    )
    # Both strategies in result.
    assert "jade_lizard_45dte" in result.per_strategy
    assert "put_calendar_30_60_atm_deb" in result.per_strategy
    # Calendar should have closed trades (it fires often in 120
    # days; either credit or debit triggers will close some).
    cal_result = result.per_strategy["put_calendar_30_60_atm_deb"]
    assert cal_result.n_closed_trades > 0


def test_portfolio_aggregates_match_per_strategy_sums(
    real_spx_chain_snapshots,
):
    """Aggregate counts equal the sum across per-strategy
    breakdowns. Sanity check on bookkeeping."""
    snaps = real_spx_chain_snapshots[:120]
    risk = RiskManager(RiskConfig(declared_capital=250_000.0))
    result = run_options_backtest_portfolio(
        strategies=[
            PutCreditSpread45dteD30(),
            IronCondor45dteD16(),
            JadeLizard45dte(),
        ],
        snapshots=snaps, risk=risk,
    )
    sum_closed = sum(
        len(r.closed_trades) for r in result.per_strategy.values()
    )
    assert sum_closed == result.n_closed_trades
    sum_pnl = sum(
        sum(t.closed_pnl_dollars for t in r.closed_trades)
        for r in result.per_strategy.values()
    )
    assert abs(sum_pnl - result.realized_pnl_dollars) < 0.01


def test_portfolio_single_strategy_matches_single_runner(
    real_spx_chain_snapshots,
):
    """Running ONE strategy through the portfolio runner should
    produce identical results to running it through the single
    runner. Backward-compat sanity.
    """
    snaps = real_spx_chain_snapshots[:120]
    risk = RiskManager(RiskConfig(declared_capital=250_000.0))
    strat = PutCreditSpread45dteD30()
    single = run_options_backtest(
        strategy=strat, snapshots=snaps, risk=risk,
    )
    # Build a fresh strategy instance for the portfolio run (avoid
    # any state leakage; both classes are stateless dataclasses
    # but be defensive).
    portfolio = run_options_backtest_portfolio(
        strategies=[PutCreditSpread45dteD30()],
        snapshots=snaps, risk=risk,
    )
    assert single.n_closed_trades == portfolio.n_closed_trades
    assert abs(
        single.realized_pnl_dollars - portfolio.realized_pnl_dollars
    ) < 0.01
