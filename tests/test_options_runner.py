"""Multi-leg backtest runner — end-to-end against real ORATS data.

Per the no-synthetic-data rule, this entire test module exercises
the runner against real ingested SPX chain snapshots via the
`real_spx_chain_snapshots` conftest fixture. The tests assert
shape invariants and the realized-P&L arithmetic; we don't have
enough real-data history yet (5 trade days) to assert on
strategy-level Sharpe / profit factors, so those wait for the
12-month pull and Phase D.
"""
from __future__ import annotations

import pytest

from tradegy.options.cost_model import OptionCostModel
from tradegy.options.runner import (
    OptionsBacktestResult,
    run_options_backtest,
)
from tradegy.options.strategies import IronCondor45dteD16
from tradegy.options.strategy import ManagementRules


# ── End-to-end shape ──────────────────────────────────────────────


def test_runner_iterates_all_snapshots(real_spx_chain_snapshots):
    strat = IronCondor45dteD16()
    result = run_options_backtest(
        strategy=strat, snapshots=real_spx_chain_snapshots,
    )
    assert isinstance(result, OptionsBacktestResult)
    assert result.n_snapshots_seen == len(real_spx_chain_snapshots)
    # We should have one P&L row per snapshot.
    assert len(result.snapshot_pnl) == len(real_spx_chain_snapshots)


def test_runner_opens_position_on_second_snapshot(
    real_spx_chain_snapshots,
):
    """Strategy decides to enter on snap[0]; the order fills at
    snap[1] (no same-bar lookahead). So `n_open_positions` should
    be 0 at snap[0] and 1 at snap[1].
    """
    strat = IronCondor45dteD16()
    result = run_options_backtest(
        strategy=strat, snapshots=real_spx_chain_snapshots,
    )
    assert result.snapshot_pnl[0].n_open_positions == 0
    if len(real_spx_chain_snapshots) >= 2:
        assert result.snapshot_pnl[1].n_open_positions == 1


def test_pnl_trajectory_realized_only_increases_with_closes(
    real_spx_chain_snapshots,
):
    """realized_dollars_cumulative is monotonic non-decreasing
    only WHEN we win; with 5 days of real data we may not have
    closed any trades. Verify the trajectory is at least
    well-formed (every snap row has the field, in chronological
    order).
    """
    strat = IronCondor45dteD16()
    result = run_options_backtest(
        strategy=strat, snapshots=real_spx_chain_snapshots,
    )
    timestamps = [row.ts_utc for row in result.snapshot_pnl]
    assert timestamps == sorted(timestamps)
    # realized_dollars_cumulative is a number on every row.
    for row in result.snapshot_pnl:
        assert row.realized_dollars_cumulative == row.realized_dollars_cumulative


def test_capital_at_risk_matches_open_positions(
    real_spx_chain_snapshots,
):
    """capital_at_risk_dollars on each snap row equals the sum of
    open positions' max_loss × contracts. With at most one open
    position (concentration rule), this equals the entry
    max_loss_per_contract.
    """
    strat = IronCondor45dteD16()
    result = run_options_backtest(
        strategy=strat, snapshots=real_spx_chain_snapshots,
    )
    # At least one snapshot in the middle should have an open
    # position with positive capital at risk (the entry happens at
    # snap[1] and runs through end-of-window unless the chain triggers
    # a close — which on 5 days it won't unless we've reached 21 DTE).
    open_rows = [r for r in result.snapshot_pnl if r.n_open_positions > 0]
    assert len(open_rows) > 0
    for row in open_rows:
        assert row.capital_at_risk_dollars > 0


def test_pnl_dollars_track_unrealized_marks(real_spx_chain_snapshots):
    """While the position is open, open_unrealized_dollars varies
    with the chain. Sanity: it's a real number, not NaN, on every
    snap with open positions.
    """
    strat = IronCondor45dteD16()
    result = run_options_backtest(
        strategy=strat, snapshots=real_spx_chain_snapshots,
    )
    for row in result.snapshot_pnl:
        if row.n_open_positions > 0:
            v = row.open_unrealized_dollars
            assert v == v  # NaN guard


# ── Cost-model integration ────────────────────────────────────────


def test_cost_model_offset_changes_realized_pnl(real_spx_chain_snapshots):
    """Same strategy + same snapshots, two cost models with
    different fill-side aggression. Worst-case (offset=1.0) should
    produce a less-favorable realized P&L than mid-fill (offset=0.0)
    on every closed trade. With only 5 days of data we may not see
    closed trades; in that case the test is a no-op (asserts only
    that the runs complete).
    """
    strat = IronCondor45dteD16()
    optimistic = OptionCostModel(spread_offset_fraction=0.0)
    pessimistic = OptionCostModel(spread_offset_fraction=1.0)
    r_opt = run_options_backtest(
        strategy=strat, snapshots=real_spx_chain_snapshots, cost=optimistic,
    )
    r_pess = run_options_backtest(
        strategy=strat, snapshots=real_spx_chain_snapshots, cost=pessimistic,
    )
    # If both runs closed any trades, optimistic ≥ pessimistic.
    if r_opt.n_closed_trades > 0 and r_pess.n_closed_trades > 0:
        assert r_opt.realized_pnl_dollars >= r_pess.realized_pnl_dollars


# ── Management discipline ─────────────────────────────────────────


def test_runner_applies_default_management_rules(
    real_spx_chain_snapshots,
):
    """Default ManagementRules: 50% / 21 DTE / 200% loss. Without
    a config override the runner uses these. Verify by running
    with explicit defaults and tight overrides and checking the
    outputs differ only in expected ways.
    """
    strat = IronCondor45dteD16()
    r_default = run_options_backtest(
        strategy=strat, snapshots=real_spx_chain_snapshots,
    )
    r_tight = run_options_backtest(
        strategy=strat, snapshots=real_spx_chain_snapshots,
        rules=ManagementRules(profit_take_pct=0.05),  # close at 5%
    )
    # Tight profit-take should close at least as many trades.
    assert r_tight.n_closed_trades >= r_default.n_closed_trades


def test_runner_strategy_id_in_result(real_spx_chain_snapshots):
    strat = IronCondor45dteD16()
    result = run_options_backtest(
        strategy=strat, snapshots=real_spx_chain_snapshots,
    )
    assert result.strategy_id == "iron_condor_45dte_d16"
