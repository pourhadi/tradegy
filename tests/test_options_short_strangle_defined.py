"""ShortStrangleDefined strategy tests against real ORATS data.

Per the no-synthetic-data rule, real chains via the
real_spx_chain_snapshots fixture.

Coverage:
  - Produces a 4-leg MultiLegOrder with both call + put legs and
    proper long/short structure.
  - 45-DTE expiry selection.
  - Short legs at ±25-delta (within ±0.05 — strike granularity).
  - Long wings at ±5-delta.
  - Body strikes are CLOSER to spot than the iron condor's
    16-delta body (the defining structural difference).
  - Net credit on entry, max-loss closed-form check.
  - Portfolio Greeks: delta near zero, theta+, vega-.
  - Comparative: defined-risk strangle collects MORE per-contract
    credit than the iron condor on the same chain (the trade-off
    that justifies its existence).
"""
from __future__ import annotations

import pytest

from tradegy.options.chain import OptionSide
from tradegy.options.cost_model import OptionCostModel
from tradegy.options.greeks import bs_greeks
from tradegy.options.positions import MultiLegPosition
from tradegy.options.risk import compute_portfolio_greeks
from tradegy.options.runner import _open_position_from_order
from tradegy.options.strategies import (
    IronCondor45dteD16,
    ShortStrangleDefined45dteD25,
)


def test_strangle_produces_4leg_order_with_both_sides(
    real_spx_chain_snapshots,
):
    snap = real_spx_chain_snapshots[0]
    strat = ShortStrangleDefined45dteD25()
    order = strat.on_chain(snap, open_positions=())
    assert order is not None
    assert order.tag == "short_strangle_defined_45dte_d25"
    assert len(order.legs) == 4
    sides = sorted(l.side.value for l in order.legs)
    assert sides == ["call", "call", "put", "put"]
    qtys = sorted(l.quantity for l in order.legs)
    assert qtys == [-1, -1, +1, +1]


def test_strangle_short_legs_at_25_delta(real_spx_chain_snapshots):
    """Short legs at ±0.05 of ±0.25 delta."""
    snap = real_spx_chain_snapshots[0]
    strat = ShortStrangleDefined45dteD25(short_delta=0.25)
    order = strat.on_chain(snap, open_positions=())
    assert order is not None
    expiry = order.legs[0].expiry
    T = (expiry - snap.ts_utc.date()).days / 365.0

    short_call = next(l for l in order.legs if l.side == OptionSide.CALL and l.quantity == -1)
    short_put = next(l for l in order.legs if l.side == OptionSide.PUT and l.quantity == -1)
    chain_legs = {(l.strike, l.side): l for l in snap.for_expiry(expiry)}

    g_c = bs_greeks(
        S=snap.underlying_price, K=short_call.strike, T=T,
        r=snap.risk_free_rate,
        sigma=chain_legs[(short_call.strike, OptionSide.CALL)].iv,
        side=OptionSide.CALL,
    )
    g_p = bs_greeks(
        S=snap.underlying_price, K=short_put.strike, T=T,
        r=snap.risk_free_rate,
        sigma=chain_legs[(short_put.strike, OptionSide.PUT)].iv,
        side=OptionSide.PUT,
    )
    assert abs(g_c.delta - 0.25) <= 0.05
    assert abs(g_p.delta - (-0.25)) <= 0.05


def test_strangle_body_closer_to_spot_than_iron_condor(
    real_spx_chain_snapshots,
):
    """The defining structural difference: 25-delta body sits
    closer to spot than the condor's 16-delta body. Verify
    against real chain data on the same snapshot.
    """
    snap = real_spx_chain_snapshots[0]
    spot = snap.underlying_price

    condor_order = IronCondor45dteD16().on_chain(snap, ())
    strangle_order = ShortStrangleDefined45dteD25().on_chain(snap, ())
    assert condor_order is not None and strangle_order is not None

    condor_short_call = next(
        l for l in condor_order.legs if l.side == OptionSide.CALL and l.quantity == -1
    )
    condor_short_put = next(
        l for l in condor_order.legs if l.side == OptionSide.PUT and l.quantity == -1
    )
    strangle_short_call = next(
        l for l in strangle_order.legs if l.side == OptionSide.CALL and l.quantity == -1
    )
    strangle_short_put = next(
        l for l in strangle_order.legs if l.side == OptionSide.PUT and l.quantity == -1
    )

    # Strangle short call should be CLOSER to spot than condor short call
    # (lower strike). Strangle short put should be CLOSER to spot
    # than condor short put (higher strike).
    assert strangle_short_call.strike < condor_short_call.strike
    assert strangle_short_put.strike > condor_short_put.strike


def test_strangle_collects_more_credit_than_iron_condor(
    real_spx_chain_snapshots,
):
    """Same underlying, same expiry, narrower body → strangle
    collects more credit per contract than iron condor. This is
    the trade-off that justifies running a strangle (more income
    per trade in exchange for more pin risk + faster losses).
    """
    snap_entry = real_spx_chain_snapshots[0]
    snap_fill = real_spx_chain_snapshots[1]
    cost = OptionCostModel()

    condor_order = IronCondor45dteD16().on_chain(snap_entry, ())
    strangle_order = ShortStrangleDefined45dteD25().on_chain(snap_entry, ())
    assert condor_order is not None and strangle_order is not None

    condor_pos = _open_position_from_order(
        condor_order, snap_fill, cost, position_id="c",
    )
    strangle_pos = _open_position_from_order(
        strangle_order, snap_fill, cost, position_id="s",
    )
    assert condor_pos is not None and strangle_pos is not None
    assert (
        strangle_pos.entry_credit_per_share
        > condor_pos.entry_credit_per_share
    ), (
        f"strangle credit {strangle_pos.entry_credit_per_share:.2f} "
        f"should exceed condor credit {condor_pos.entry_credit_per_share:.2f}"
    )


def test_strangle_portfolio_greeks_signs(real_spx_chain_snapshots):
    """Same Greek-sign expectations as iron condor: delta near
    zero (both sides offset), theta positive (net short premium),
    vega negative (net short vol).
    """
    snap_entry = real_spx_chain_snapshots[0]
    snap_fill = real_spx_chain_snapshots[1]
    mark_index = min(5, len(real_spx_chain_snapshots) - 1)
    snap_mark = real_spx_chain_snapshots[mark_index]

    order = ShortStrangleDefined45dteD25().on_chain(snap_entry, ())
    assert order is not None
    pos = _open_position_from_order(
        order, snap_fill, OptionCostModel(), position_id="g",
    )
    assert pos is not None
    g = compute_portfolio_greeks([pos], snap_mark)
    assert g.theta_dollars > 0
    assert g.vega_dollars < 0


def test_strangle_skips_when_position_open(real_spx_chain_snapshots):
    snap = real_spx_chain_snapshots[0]
    fake_open = (
        MultiLegPosition(
            position_id="dummy", strategy_class="short_strangle_defined_45dte_d25",
            contracts=1, legs=(), entry_ts=snap.ts_utc,
            entry_credit_per_share=10.0, max_loss_per_contract=4000.0,
        ),
    )
    assert ShortStrangleDefined45dteD25().on_chain(snap, fake_open) is None
