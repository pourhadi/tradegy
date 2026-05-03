"""RiskManager + portfolio Greeks tests against real ORATS data.

Per the no-synthetic-data rule, every test below uses real
ingested SPX chain snapshots via the `real_spx_chain_snapshots`
conftest fixture and real positions opened by the runner against
that data.

Coverage:

  - Portfolio Greeks aggregation across real open positions.
  - Capital cap rejects when proposed > declared * pct.
  - Capital cap approves when proposed ≤ declared * pct.
  - Per-expiration cap rejects when too much sits in one cycle.
  - Tail-event halt returns None when history < min_history (5
    snapshots in fixture is < 63 — correct default behavior, no
    spurious halt on day one).
  - Runner integration: RiskManager-rejected orders are recorded
    in result.rejected_orders with the gating reason; no position
    opens when rejected.
  - End-to-end: $25K capital cap correctly prevents the
    IronCondor45dteD16 from opening the $48K-at-risk position
    that B-2's smoke test surfaced.
"""
from __future__ import annotations

import pytest

from tradegy.options.cost_model import OptionCostModel
from tradegy.options.risk import (
    PortfolioGreeks,
    RiskConfig,
    RiskManager,
    compute_portfolio_greeks,
)
from tradegy.options.runner import run_options_backtest
from tradegy.options.strategies import IronCondor45dteD16


# ── End-to-end: B-2 finding addressed ──────────────────────────


def test_25k_capital_cap_blocks_48k_iron_condor(real_spx_chain_snapshots):
    """The exact failure mode from the B-2 smoke test:
    IronCondor45dteD16 wants to enter a position with ~$48K at risk
    against a $25K target capital. With a 50% cap that's $12.5K
    max — the order must be rejected.
    """
    strat = IronCondor45dteD16()
    risk = RiskManager(RiskConfig(
        declared_capital=25_000.0,
        max_capital_at_risk_pct=0.50,
        max_per_expiration_pct=0.25,
    ))
    result = run_options_backtest(
        strategy=strat, snapshots=real_spx_chain_snapshots, risk=risk,
    )
    assert len(result.rejected_orders) >= 1
    # First rejection should cite capital cap.
    first = result.rejected_orders[0]
    assert "capital_cap" in first.reason
    # No position opened across the whole window.
    for row in result.snapshot_pnl:
        assert row.n_open_positions == 0
    assert result.n_closed_trades == 0


def test_high_capital_lets_position_open(real_spx_chain_snapshots):
    """Same strategy, $250K declared (10x). 50% cap = $125K — the
    $48K condor fits comfortably. Position should open as in B-2.
    """
    strat = IronCondor45dteD16()
    risk = RiskManager(RiskConfig(
        declared_capital=250_000.0,
        max_capital_at_risk_pct=0.50,
        max_per_expiration_pct=0.50,
    ))
    result = run_options_backtest(
        strategy=strat, snapshots=real_spx_chain_snapshots, risk=risk,
    )
    # No risk rejections.
    assert all(
        "capital_cap" not in r.reason for r in result.rejected_orders
    )
    # Position eventually opens.
    has_open = any(row.n_open_positions > 0 for row in result.snapshot_pnl)
    assert has_open


# ── Per-expiration cap ─────────────────────────────────────────


def test_per_expiration_cap_rejects_concentration(real_spx_chain_snapshots):
    """Set the per-expiration cap to a value below the proposed
    position's max-loss per contract. Even with plenty of total
    capital headroom, the per-expiration check must reject.
    """
    strat = IronCondor45dteD16()
    risk = RiskManager(RiskConfig(
        declared_capital=1_000_000.0,           # huge total budget
        max_capital_at_risk_pct=0.99,           # essentially no total cap
        max_per_expiration_pct=0.001,           # tiny per-expiration cap
                                                # (= $1,000)
    ))
    result = run_options_backtest(
        strategy=strat, snapshots=real_spx_chain_snapshots, risk=risk,
    )
    # The condor's max-loss is ~$48K; per-expiration cap is $1K.
    # First fill attempt should reject.
    rejections = [
        r for r in result.rejected_orders
        if "per_expiration_cap" in r.reason
    ]
    assert len(rejections) >= 1


# ── Tail-event halt — short history ────────────────────────────


def test_tail_event_halt_inactive_at_low_threshold_history(
    real_spx_chain_snapshots,
):
    """The halt is OFF for the first `min_history_for_rv_halt`
    snapshots regardless of regime — without sufficient history,
    realized-vol percentile can't be computed and we default to
    permissive (no halt) so day-1 backtests don't false-halt on
    insufficient data.

    We verify by setting min_history_for_rv_halt larger than the
    fixture's snapshot count: regardless of how many real days
    we have, the halt cannot fire because history < minimum.
    """
    strat = IronCondor45dteD16()
    risk = RiskManager(RiskConfig(
        declared_capital=250_000.0,
        max_capital_at_risk_pct=0.50,
        max_per_expiration_pct=0.50,
        suspend_above_rv_pct=0.95,
        min_history_for_rv_halt=10_000,  # impossibly large — disables halt
    ))
    result = run_options_backtest(
        strategy=strat, snapshots=real_spx_chain_snapshots, risk=risk,
    )
    assert not any(
        "tail_event_halt" in r.reason for r in result.rejected_orders
    )


# ── Portfolio Greeks ───────────────────────────────────────────


def test_portfolio_greeks_zero_when_no_positions(real_spx_chain_snapshots):
    snap = real_spx_chain_snapshots[0]
    g = compute_portfolio_greeks([], snap)
    assert g == PortfolioGreeks()


def test_portfolio_greeks_after_real_iron_condor_entry(
    real_spx_chain_snapshots,
):
    """Open the iron condor in a real backtest, take the Greeks at
    a later snap, verify they're real (non-NaN) numbers with
    sensible signs.

    Iron condor with both short body legs is approximately
    delta-neutral at entry (short call delta + short put delta ≈
    0); we should see |delta_dollars| reasonably small relative
    to position size. Theta should be POSITIVE (we're net short
    premium → benefit from time decay). Vega should be NEGATIVE
    (we're short vol → hurt by IV expansion).
    """
    strat = IronCondor45dteD16()
    risk = RiskManager(RiskConfig(
        declared_capital=250_000.0,
        max_capital_at_risk_pct=0.50,
        max_per_expiration_pct=0.50,
    ))
    # Run; pull the open position out of the runner state via
    # snap_pnl + reconstruct via direct access.
    # We compute Greeks against snap[5] (within the position's
    # 45-DTE lifecycle, regardless of whether the fixture has 5
    # days or 250+). Marking against snap[-1] would mark a Jan-
    # opened position against Dec which is past expiry.
    snap_entry = real_spx_chain_snapshots[0]
    snap_fill = real_spx_chain_snapshots[1]
    mark_index = min(5, len(real_spx_chain_snapshots) - 1)
    snap_mark = real_spx_chain_snapshots[mark_index]

    order = strat.on_chain(snap_entry, ())
    assert order is not None
    from tradegy.options.runner import _open_position_from_order
    cost = OptionCostModel()
    pos = _open_position_from_order(
        order, snap_fill, cost, position_id="greeks_test",
    )
    assert pos is not None

    g = compute_portfolio_greeks([pos], snap_mark)
    # All Greeks finite numbers.
    assert g.delta_dollars == g.delta_dollars
    assert g.gamma_dollars == g.gamma_dollars
    assert g.theta_dollars == g.theta_dollars
    assert g.vega_dollars == g.vega_dollars
    # Net-short-premium signs.
    assert g.theta_dollars > 0, (
        "iron condor is net short premium → theta should be POSITIVE "
        f"(time decay benefits us); got {g.theta_dollars:.2f}"
    )
    assert g.vega_dollars < 0, (
        "iron condor is net short vol → vega should be NEGATIVE "
        f"(IV expansion hurts); got {g.vega_dollars:.2f}"
    )


# ── Rejected-order audit trail ─────────────────────────────────


def test_rejected_orders_include_gating_metadata(
    real_spx_chain_snapshots,
):
    """RejectedOrder rows carry the strategy tag, attempted-fill
    timestamp, and capital-at-risk number that would have been
    deployed. Audit-trail completeness check.
    """
    strat = IronCondor45dteD16()
    risk = RiskManager(RiskConfig(
        declared_capital=10_000.0,    # below condor's ~$48K
        max_capital_at_risk_pct=0.50,
        max_per_expiration_pct=0.50,
    ))
    result = run_options_backtest(
        strategy=strat, snapshots=real_spx_chain_snapshots, risk=risk,
    )
    assert len(result.rejected_orders) >= 1
    rec = result.rejected_orders[0]
    assert rec.strategy_tag == "iron_condor_45dte_d16"
    assert rec.proposed_capital_at_risk > 0
    assert rec.fill_attempted_ts is not None


def test_no_risk_manager_preserves_phase_b2_behavior(
    real_spx_chain_snapshots,
):
    """When risk=None, the runner behaves identically to Phase B-2
    (no capital cap, no rejection). Backward-compat check so
    existing callers don't silently change behavior.
    """
    strat = IronCondor45dteD16()
    result = run_options_backtest(
        strategy=strat, snapshots=real_spx_chain_snapshots, risk=None,
    )
    assert len(result.rejected_orders) == 0
    has_open = any(row.n_open_positions > 0 for row in result.snapshot_pnl)
    assert has_open
