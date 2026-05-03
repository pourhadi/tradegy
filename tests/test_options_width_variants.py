"""Width-anchored vs delta-anchored wing-selection tests.

Per the no-synthetic-data rule. Verifies the dual-mode wing
parameter on PutCreditSpread, CallCreditSpread, IronCondor, and
ShortStrangleDefined.

Coverage:
  - When wing_width_dollars is None (default), behavior matches
    the prior delta-anchored test results — no regression on
    existing strategy id.
  - When wing_width_dollars is set:
      * The selected long-wing strike is approximately
        `short_strike ± wing_width_dollars`.
      * The resulting spread width is much smaller than the
        delta-anchored default (~$25-50 vs $400-775 on SPX).
      * Credit/risk ratio improves substantially (typically 20-30%
        vs the 9-12% delta-anchored numbers).
"""
from __future__ import annotations

import pytest

from tradegy.options.chain import OptionSide
from tradegy.options.cost_model import OptionCostModel
from tradegy.options.runner import _open_position_from_order
from tradegy.options.strategies import (
    CallCreditSpread45dteD30,
    IronCondor45dteD16,
    PutCreditSpread45dteD30,
    ShortStrangleDefined45dteD25,
)


# ── PutCreditSpread width variant ──────────────────────────────


def test_pcs_width_variant_selects_strike_at_offset(real_spx_chain_snapshots):
    """With wing_width_dollars=$25, long put strike sits ~$25
    below short put strike (SPX strike granularity is $5 around
    ATM, so closest available may be $20 or $25 — within $5).
    """
    snap = real_spx_chain_snapshots[0]
    strat = PutCreditSpread45dteD30(wing_width_dollars=25.0)
    order = strat.on_chain(snap, ())
    assert order is not None
    short_put = next(l for l in order.legs if l.quantity == -1)
    long_put = next(l for l in order.legs if l.quantity == +1)
    width = short_put.strike - long_put.strike
    assert 20 <= width <= 30, f"expected ~$25-wide; got ${width}"


def test_pcs_width_variant_improves_credit_to_risk_ratio(
    real_spx_chain_snapshots,
):
    """Width-anchored variant produces a higher credit/risk ratio
    than delta-anchored because the wing is much narrower (and so
    is max loss). For PCS specifically, the absolute c/r is still
    modest (~10%) because a 30-delta short on SPX sits far OTM —
    real-data finding 2026-05-03: PCS delta 3.7% c/r → PCS width
    $25 is 10.8% c/r (3x improvement, but still modest absolute).
    See test_ic_* for the much-larger improvement that Iron Condor
    achieves (delta 13% → width $50 76%) when both sides
    contribute credit.
    """
    snap_entry = real_spx_chain_snapshots[0]
    snap_fill = real_spx_chain_snapshots[1]
    cost = OptionCostModel()

    delta_pos = _open_position_from_order(
        PutCreditSpread45dteD30().on_chain(snap_entry, ()),
        snap_fill, cost, position_id="d",
    )
    width_pos = _open_position_from_order(
        PutCreditSpread45dteD30(wing_width_dollars=25.0).on_chain(
            snap_entry, (),
        ),
        snap_fill, cost, position_id="w",
    )
    assert delta_pos is not None and width_pos is not None
    delta_ratio = delta_pos.entry_credit_dollars / delta_pos.max_loss_per_contract
    width_ratio = width_pos.entry_credit_dollars / width_pos.max_loss_per_contract
    # Relative improvement asserts the design intent without baking
    # in a specific market-regime number.
    assert width_ratio > delta_ratio * 2.0, (
        f"width-anchored c/r {width_ratio:.3f} should be ≥ 2x delta-"
        f"anchored {delta_ratio:.3f}; got ratio {width_ratio/delta_ratio:.2f}x"
    )
    # Max loss MUST be smaller (the structural reason c/r improves).
    assert width_pos.max_loss_per_contract < delta_pos.max_loss_per_contract


def test_pcs_default_unchanged_without_width_param(real_spx_chain_snapshots):
    """Backward compat: PutCreditSpread() with no wing_width_dollars
    behaves identically to before (delta-anchored).
    """
    snap = real_spx_chain_snapshots[0]
    a = PutCreditSpread45dteD30().on_chain(snap, ())
    b = PutCreditSpread45dteD30(wing_width_dollars=None).on_chain(snap, ())
    assert a is not None and b is not None
    a_strikes = sorted((l.strike, l.quantity) for l in a.legs)
    b_strikes = sorted((l.strike, l.quantity) for l in b.legs)
    assert a_strikes == b_strikes


# ── CallCreditSpread width variant ─────────────────────────────


def test_ccs_width_variant_selects_strike_above_short(
    real_spx_chain_snapshots,
):
    snap = real_spx_chain_snapshots[0]
    order = CallCreditSpread45dteD30(wing_width_dollars=25.0).on_chain(snap, ())
    assert order is not None
    short_call = next(l for l in order.legs if l.quantity == -1)
    long_call = next(l for l in order.legs if l.quantity == +1)
    width = long_call.strike - short_call.strike
    assert 20 <= width <= 30


# ── IronCondor width variant ───────────────────────────────────


def test_ic_width_variant_both_wings_at_offset(real_spx_chain_snapshots):
    """IC with wing_width_dollars=$50: both wings sit ~$50 from
    their respective body strikes.
    """
    snap = real_spx_chain_snapshots[0]
    order = IronCondor45dteD16(wing_width_dollars=50.0).on_chain(snap, ())
    assert order is not None
    short_put = next(l for l in order.legs if l.side == OptionSide.PUT and l.quantity == -1)
    long_put = next(l for l in order.legs if l.side == OptionSide.PUT and l.quantity == +1)
    short_call = next(l for l in order.legs if l.side == OptionSide.CALL and l.quantity == -1)
    long_call = next(l for l in order.legs if l.side == OptionSide.CALL and l.quantity == +1)
    put_width = short_put.strike - long_put.strike
    call_width = long_call.strike - short_call.strike
    assert 45 <= put_width <= 55
    assert 45 <= call_width <= 55


def test_ic_width_variant_max_loss_smaller_than_delta_default(
    real_spx_chain_snapshots,
):
    """Width-anchored IC has narrower wings → smaller max loss per
    contract than delta-anchored default. This matters for
    capital efficiency.
    """
    snap_entry = real_spx_chain_snapshots[0]
    snap_fill = real_spx_chain_snapshots[1]
    cost = OptionCostModel()
    delta_pos = _open_position_from_order(
        IronCondor45dteD16().on_chain(snap_entry, ()),
        snap_fill, cost, position_id="d",
    )
    width_pos = _open_position_from_order(
        IronCondor45dteD16(wing_width_dollars=50.0).on_chain(snap_entry, ()),
        snap_fill, cost, position_id="w",
    )
    assert delta_pos is not None and width_pos is not None
    assert (
        width_pos.max_loss_per_contract < delta_pos.max_loss_per_contract
    )


def test_ic_width_variant_dramatically_improves_credit_to_risk(
    real_spx_chain_snapshots,
):
    """Iron condor with width-anchored wings shows the largest c/r
    improvement of any width variant — both sides contribute
    credit while max-loss is bounded by the smaller wing width.
    Real-data finding 2026-05-03: IC delta-anchored c/r 13.0% →
    IC width-$50 c/r 76.3% (~6x improvement). Assert the
    improvement is at least 3x to avoid baking in regime numbers.
    """
    snap_entry = real_spx_chain_snapshots[0]
    snap_fill = real_spx_chain_snapshots[1]
    cost = OptionCostModel()
    delta_pos = _open_position_from_order(
        IronCondor45dteD16().on_chain(snap_entry, ()),
        snap_fill, cost, position_id="d",
    )
    width_pos = _open_position_from_order(
        IronCondor45dteD16(wing_width_dollars=50.0).on_chain(snap_entry, ()),
        snap_fill, cost, position_id="w",
    )
    delta_cr = delta_pos.entry_credit_dollars / delta_pos.max_loss_per_contract
    width_cr = width_pos.entry_credit_dollars / width_pos.max_loss_per_contract
    assert width_cr > delta_cr * 3.0, (
        f"IC width c/r {width_cr:.3f} should be ≥ 3x delta {delta_cr:.3f}; "
        f"got ratio {width_cr/delta_cr:.2f}x"
    )
    # Sanity: width-anchored IC c/r should be in practitioner range.
    assert width_cr > 0.30, (
        f"IC width c/r {width_cr:.3f} below 30% — width may be too "
        "tight or chain too thin"
    )


# ── ShortStrangle width variant ────────────────────────────────


def test_strangle_width_variant_both_wings_at_offset(
    real_spx_chain_snapshots,
):
    snap = real_spx_chain_snapshots[0]
    order = ShortStrangleDefined45dteD25(wing_width_dollars=50.0).on_chain(snap, ())
    assert order is not None
    short_put = next(l for l in order.legs if l.side == OptionSide.PUT and l.quantity == -1)
    long_put = next(l for l in order.legs if l.side == OptionSide.PUT and l.quantity == +1)
    short_call = next(l for l in order.legs if l.side == OptionSide.CALL and l.quantity == -1)
    long_call = next(l for l in order.legs if l.side == OptionSide.CALL and l.quantity == +1)
    assert 45 <= short_put.strike - long_put.strike <= 55
    assert 45 <= long_call.strike - short_call.strike <= 55
