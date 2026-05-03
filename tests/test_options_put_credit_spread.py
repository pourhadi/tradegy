"""PutCreditSpread strategy class tests against real ORATS data.

Per the no-synthetic-data rule, every test exercises real ingested
SPX chain data via the `real_spx_chain_snapshots` fixture.

Coverage:

  - Produces a 2-leg MultiLegOrder on a real SPX chain.
  - Both legs are PUTs at the chosen 45-DTE expiry.
  - Short put delta is ~-0.30 (within ±0.05 — closest-strike
    granularity on SPX).
  - Long wing delta is ~-0.05.
  - Long put strike < short put strike (positive spread width).
  - Skips when concentration is already taken (one open position
    per strategy instance).
  - Net premium is a CREDIT (we receive on entry).
  - End-to-end through the runner produces a real position with
    sensible portfolio Greeks (positive delta = bullish bias,
    positive theta = short premium, negative vega = short vol).
"""
from __future__ import annotations

import pytest

from tradegy.options.chain import OptionSide
from tradegy.options.cost_model import OptionCostModel
from tradegy.options.greeks import bs_greeks
from tradegy.options.positions import MultiLegPosition
from tradegy.options.risk import compute_portfolio_greeks
from tradegy.options.runner import _open_position_from_order, run_options_backtest
from tradegy.options.strategies import PutCreditSpread45dteD30


# ── Strategy-side leg selection ────────────────────────────────


def test_pcs_produces_2leg_put_only_order(real_spx_chain_snapshots):
    snap = real_spx_chain_snapshots[0]
    strat = PutCreditSpread45dteD30()
    order = strat.on_chain(snap, open_positions=())
    assert order is not None
    assert order.tag == "put_credit_spread_45dte_d30"
    assert len(order.legs) == 2
    sides = [l.side for l in order.legs]
    assert sides == [OptionSide.PUT, OptionSide.PUT]
    quantities = sorted([l.quantity for l in order.legs])
    assert quantities == [-1, +1]


def test_pcs_picks_45dte_expiry(real_spx_chain_snapshots):
    snap = real_spx_chain_snapshots[0]
    strat = PutCreditSpread45dteD30(target_dte=45)
    order = strat.on_chain(snap, open_positions=())
    assert order is not None
    expiry = order.legs[0].expiry
    dte = (expiry - snap.ts_utc.date()).days
    assert 30 <= dte <= 60, f"expected ~45 DTE, got {dte}"


def test_pcs_long_strike_below_short_strike(real_spx_chain_snapshots):
    """The long-put protection MUST sit below the short-put body.
    Otherwise the position isn't a credit spread.
    """
    snap = real_spx_chain_snapshots[0]
    strat = PutCreditSpread45dteD30()
    order = strat.on_chain(snap, open_positions=())
    assert order is not None
    long_put = next(l for l in order.legs if l.quantity == +1)
    short_put = next(l for l in order.legs if l.quantity == -1)
    assert long_put.strike < short_put.strike


def test_pcs_short_put_at_target_delta(real_spx_chain_snapshots):
    """Short put delta should be within ±0.05 of -0.30."""
    snap = real_spx_chain_snapshots[0]
    strat = PutCreditSpread45dteD30(short_delta=0.30)
    order = strat.on_chain(snap, open_positions=())
    assert order is not None
    short_put_order = next(l for l in order.legs if l.quantity == -1)
    expiry = short_put_order.expiry
    T = (expiry - snap.ts_utc.date()).days / 365.0

    chain_legs = {(l.strike, l.side): l for l in snap.for_expiry(expiry)}
    short_put_chain = chain_legs[(short_put_order.strike, OptionSide.PUT)]
    g = bs_greeks(
        S=snap.underlying_price, K=short_put_chain.strike, T=T,
        r=snap.risk_free_rate, sigma=short_put_chain.iv,
        side=OptionSide.PUT,
    )
    assert abs(g.delta - (-0.30)) <= 0.05


def test_pcs_long_wing_at_target_delta(real_spx_chain_snapshots):
    """Long-wing delta should be within ±0.04 of -0.05."""
    snap = real_spx_chain_snapshots[0]
    strat = PutCreditSpread45dteD30(wing_delta=0.05)
    order = strat.on_chain(snap, open_positions=())
    assert order is not None
    long_put_order = next(l for l in order.legs if l.quantity == +1)
    expiry = long_put_order.expiry
    T = (expiry - snap.ts_utc.date()).days / 365.0

    chain_legs = {(l.strike, l.side): l for l in snap.for_expiry(expiry)}
    long_put_chain = chain_legs[(long_put_order.strike, OptionSide.PUT)]
    g = bs_greeks(
        S=snap.underlying_price, K=long_put_chain.strike, T=T,
        r=snap.risk_free_rate, sigma=long_put_chain.iv,
        side=OptionSide.PUT,
    )
    assert abs(g.delta - (-0.05)) <= 0.04


def test_pcs_skips_when_position_open(real_spx_chain_snapshots):
    snap = real_spx_chain_snapshots[0]
    strat = PutCreditSpread45dteD30()
    fake_open = (
        MultiLegPosition(
            position_id="dummy", strategy_class="put_credit_spread_45dte_d30",
            contracts=1, legs=(), entry_ts=snap.ts_utc,
            entry_credit_per_share=2.0, max_loss_per_contract=4800.0,
        ),
    )
    assert strat.on_chain(snap, open_positions=fake_open) is None


# ── End-to-end through the runner on real chain ───────────────


def test_pcs_opens_real_position_with_credit(real_spx_chain_snapshots):
    """Run end-to-end. The position should open and yield a NET
    CREDIT (entry_credit_per_share > 0).
    """
    strat = PutCreditSpread45dteD30()
    snap_entry = real_spx_chain_snapshots[0]
    snap_fill = real_spx_chain_snapshots[1]

    order = strat.on_chain(snap_entry, ())
    assert order is not None
    pos = _open_position_from_order(
        order, snap_fill, OptionCostModel(), position_id="pcs_test",
    )
    assert pos is not None
    assert pos.entry_credit_per_share > 0, (
        f"expected net credit; got "
        f"{pos.entry_credit_per_share:.2f}"
    )
    # Max loss = (short_strike - long_strike) * 100 - credit_per_share * 100.
    short_put = next(l for l in pos.legs if l.quantity == -1)
    long_put = next(l for l in pos.legs if l.quantity == +1)
    spread_width = short_put.strike - long_put.strike
    expected_max_loss = (
        spread_width * 100 - pos.entry_credit_per_share * 100
    )
    assert pos.max_loss_per_contract == pytest.approx(
        expected_max_loss, rel=0.01,
    )


def test_pcs_portfolio_greeks_signs(real_spx_chain_snapshots):
    """Real iron-condor entry produced delta-near-zero, theta+, vega-.
    The put credit spread should produce:
      delta POSITIVE (bullish bet on no-drop)
      theta POSITIVE (net short premium, decay benefits)
      vega NEGATIVE (net short vol)
    """
    strat = PutCreditSpread45dteD30()
    snap_entry = real_spx_chain_snapshots[0]
    snap_fill = real_spx_chain_snapshots[1]
    mark_index = min(5, len(real_spx_chain_snapshots) - 1)
    snap_mark = real_spx_chain_snapshots[mark_index]

    order = strat.on_chain(snap_entry, ())
    assert order is not None
    pos = _open_position_from_order(
        order, snap_fill, OptionCostModel(), position_id="pcs_g1",
    )
    assert pos is not None

    g = compute_portfolio_greeks([pos], snap_mark)
    assert g.delta_dollars > 0, (
        "put credit spread is bullish → delta should be POSITIVE; "
        f"got {g.delta_dollars:.2f}"
    )
    assert g.theta_dollars > 0
    assert g.vega_dollars < 0


def test_pcs_runs_through_runner_end_to_end(real_spx_chain_snapshots):
    """Full backtest invocation works (smoke). With the standard
    risk caps the position fits because the credit spread is much
    smaller than the iron condor.
    """
    from tradegy.options.risk import RiskConfig, RiskManager
    strat = PutCreditSpread45dteD30()
    risk = RiskManager(RiskConfig(
        declared_capital=25_000.0,
        max_capital_at_risk_pct=0.50,
        max_per_expiration_pct=0.50,
    ))
    result = run_options_backtest(
        strategy=strat, snapshots=real_spx_chain_snapshots, risk=risk,
    )
    # Either the position opens (capital fits) or it's rejected
    # (capital cap). Both are valid outcomes for this test —
    # confirming the runner doesn't crash on a 2-leg strategy.
    assert result.n_snapshots_seen == len(real_spx_chain_snapshots)
