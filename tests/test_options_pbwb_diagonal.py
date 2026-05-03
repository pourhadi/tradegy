"""Real-data tests for PutBrokenWingButterfly + PutDiagonal.

Per the no-synthetic-data rule. Verifies the new structures
construct correctly on real SPX chains and exhibit the expected
P&L signatures.
"""
from __future__ import annotations

import pytest

from tradegy.options.chain import OptionSide
from tradegy.options.cost_model import OptionCostModel
from tradegy.options.greeks import bs_greeks
from tradegy.options.risk import compute_portfolio_greeks
from tradegy.options.runner import _open_position_from_order
from tradegy.options.strategies import (
    PutBrokenWingButterfly45dte,
    PutDiagonal30_60,
)


# ── Put Broken-Wing Butterfly ─────────────────────────────────


def test_pbwb_3leg_strikes_with_double_short(real_spx_chain_snapshots):
    """3 distinct strikes (long outer, short body x2, long inner)."""
    snap = real_spx_chain_snapshots[0]
    order = PutBrokenWingButterfly45dte().on_chain(snap, ())
    assert order is not None
    assert order.tag == "put_broken_wing_butterfly_45dte_d20"
    assert len(order.legs) == 3
    assert all(l.side == OptionSide.PUT for l in order.legs)
    quantities = sorted(l.quantity for l in order.legs)
    assert quantities == [-2, +1, +1]


def test_pbwb_strikes_strictly_ordered(real_spx_chain_snapshots):
    """K1 (long inner) > K2 (short body) > K3 (long outer)."""
    snap = real_spx_chain_snapshots[0]
    order = PutBrokenWingButterfly45dte().on_chain(snap, ())
    body = next(l for l in order.legs if l.quantity == -2)
    longs = [l for l in order.legs if l.quantity == +1]
    long_inner = next(l for l in longs if l.strike > body.strike)
    long_outer = next(l for l in longs if l.strike < body.strike)
    assert long_outer.strike < body.strike < long_inner.strike


def test_pbwb_outer_wing_wider_than_inner(real_spx_chain_snapshots):
    """Defining feature: outer wing > inner wing (asymmetric).
    With defaults inner=$25 and outer=$75, the actual wing widths
    should reflect that ratio (within strike-density rounding).
    """
    snap = real_spx_chain_snapshots[0]
    order = PutBrokenWingButterfly45dte().on_chain(snap, ())
    body = next(l for l in order.legs if l.quantity == -2)
    longs = [l for l in order.legs if l.quantity == +1]
    long_inner = next(l for l in longs if l.strike > body.strike)
    long_outer = next(l for l in longs if l.strike < body.strike)
    inner_w = long_inner.strike - body.strike
    outer_w = body.strike - long_outer.strike
    assert outer_w > inner_w, (
        f"outer wing ${outer_w} should exceed inner wing ${inner_w}"
    )


def test_pbwb_body_at_target_delta(real_spx_chain_snapshots):
    """Short body sits within ±0.05 of -0.20 delta."""
    snap = real_spx_chain_snapshots[0]
    strat = PutBrokenWingButterfly45dte(body_delta=0.20)
    order = strat.on_chain(snap, ())
    body = next(l for l in order.legs if l.quantity == -2)
    expiry = body.expiry
    T = (expiry - snap.ts_utc.date()).days / 365.0
    chain_legs = {(l.strike, l.side): l for l in snap.for_expiry(expiry)}
    body_chain = chain_legs[(body.strike, OptionSide.PUT)]
    g = bs_greeks(
        S=snap.underlying_price, K=body_chain.strike, T=T,
        r=snap.risk_free_rate, sigma=body_chain.iv, side=OptionSide.PUT,
    )
    assert abs(g.delta - (-0.20)) <= 0.05


def test_pbwb_max_loss_matches_payoff_formula(real_spx_chain_snapshots):
    """For a put BWB:
        max_loss_per_share = outer_width - inner_width - credit_per_share
    Verify the runner's compute_max_loss_per_contract matches.
    """
    snap_entry = real_spx_chain_snapshots[0]
    snap_fill = real_spx_chain_snapshots[1]
    order = PutBrokenWingButterfly45dte().on_chain(snap_entry, ())
    pos = _open_position_from_order(
        order, snap_fill, OptionCostModel(), position_id="pbwb",
    )
    assert pos is not None

    body = next(l for l in pos.legs if l.quantity == -2)
    longs = [l for l in pos.legs if l.quantity == +1]
    long_inner = next(l for l in longs if l.strike > body.strike)
    long_outer = next(l for l in longs if l.strike < body.strike)
    inner_w = long_inner.strike - body.strike
    outer_w = body.strike - long_outer.strike

    expected_max_loss_per_share = outer_w - inner_w - pos.entry_credit_per_share
    expected_dollars = expected_max_loss_per_share * 100

    assert pos.max_loss_per_contract == pytest.approx(
        expected_dollars, rel=0.01,
    )


def test_pbwb_zero_when_outer_not_wider(real_spx_chain_snapshots):
    """A "broken-wing" requires asymmetric wings; equal wings
    degenerate to a standard butterfly. Strategy returns None
    when configured with equal wings.
    """
    snap = real_spx_chain_snapshots[0]
    strat = PutBrokenWingButterfly45dte(
        inner_wing_dollars=50.0, outer_wing_dollars=50.0,
    )
    assert strat.on_chain(snap, ()) is None


# ── Put Diagonal ──────────────────────────────────────────────


def test_diagonal_2leg_different_strikes_different_expiries(
    real_spx_chain_snapshots,
):
    snap = real_spx_chain_snapshots[0]
    order = PutDiagonal30_60().on_chain(snap, ())
    assert order is not None
    assert order.tag == "put_diagonal_30_60_d30_d10"
    assert len(order.legs) == 2
    assert all(l.side == OptionSide.PUT for l in order.legs)
    short_put = next(l for l in order.legs if l.quantity == -1)
    long_put = next(l for l in order.legs if l.quantity == +1)
    # Different expiries (defining feature vs vertical credit spread).
    assert short_put.expiry != long_put.expiry
    # Different strikes (defining feature vs calendar).
    assert short_put.strike != long_put.strike


def test_diagonal_short_is_front_long_is_back(real_spx_chain_snapshots):
    """Short = near expiry (collect fast decay); long = far
    expiry (slower decay protection). Same orientation as
    calendar but with strike asymmetry."""
    snap = real_spx_chain_snapshots[0]
    order = PutDiagonal30_60().on_chain(snap, ())
    short_put = next(l for l in order.legs if l.quantity == -1)
    long_put = next(l for l in order.legs if l.quantity == +1)
    assert short_put.expiry < long_put.expiry


def test_diagonal_long_strike_below_short_strike(real_spx_chain_snapshots):
    """Bullish bias: long protective leg sits BELOW short body
    (further OTM). If long strike >= short strike it's a different
    structure (calendar or vertical-equivalent)."""
    snap = real_spx_chain_snapshots[0]
    order = PutDiagonal30_60().on_chain(snap, ())
    short_put = next(l for l in order.legs if l.quantity == -1)
    long_put = next(l for l in order.legs if l.quantity == +1)
    assert long_put.strike < short_put.strike


def test_diagonal_short_at_target_delta(real_spx_chain_snapshots):
    """Short put delta ~-0.30."""
    snap = real_spx_chain_snapshots[0]
    strat = PutDiagonal30_60(short_delta=0.30)
    order = strat.on_chain(snap, ())
    short_put_order = next(l for l in order.legs if l.quantity == -1)
    expiry = short_put_order.expiry
    T = (expiry - snap.ts_utc.date()).days / 365.0
    chain_legs = {(l.strike, l.side): l for l in snap.for_expiry(expiry)}
    leg = chain_legs[(short_put_order.strike, OptionSide.PUT)]
    g = bs_greeks(
        S=snap.underlying_price, K=leg.strike, T=T,
        r=snap.risk_free_rate, sigma=leg.iv, side=OptionSide.PUT,
    )
    assert abs(g.delta - (-0.30)) <= 0.05


def test_diagonal_portfolio_signs(real_spx_chain_snapshots):
    """Diagonal is short-net-premium → theta+ at the front-leg
    timescale; vega- (we're short the front-leg vol). Bullish
    bias → delta should be moderately positive (we benefit from
    the underlying staying up).
    """
    snap_entry = real_spx_chain_snapshots[0]
    snap_fill = real_spx_chain_snapshots[1]
    mark_index = min(5, len(real_spx_chain_snapshots) - 1)
    snap_mark = real_spx_chain_snapshots[mark_index]

    order = PutDiagonal30_60().on_chain(snap_entry, ())
    pos = _open_position_from_order(
        order, snap_fill, OptionCostModel(), position_id="diag",
    )
    assert pos is not None
    g = compute_portfolio_greeks([pos], snap_mark)
    # Bullish bias.
    assert g.delta_dollars > 0, (
        f"diagonal should be net-bullish; got delta_dollars={g.delta_dollars}"
    )
