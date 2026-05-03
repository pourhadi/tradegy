"""PutCalendarSpread tests against real ORATS data.

Per the no-synthetic-data rule. Calendar spreads exercise the
debit-position branch of `should_close` + the new
`pnl_pct_of_debit` property. Real-data validation against the
2025-12-15 SPX chain.

Coverage:
  - Produces a 2-leg same-strike DIFFERENT-expiry put order.
  - Front expiry is closer to 30 DTE; back expiry closer to 60.
  - Same strike on both legs (defining feature of a calendar).
  - Net DEBIT on entry (entry_credit_per_share < 0).
  - Max loss equals debit paid (sampling-based math is correct
    by construction for same-strike calendars: the intrinsics
    cancel at every spot value, so worst case is the debit).
  - days_to_expiry returns the FRONT leg's DTE (load-bearing
    for the 21-DTE management trigger).
  - pnl_pct_of_debit returns a real number (not NaN) for a
    debit position; pnl_pct_of_max_credit returns NaN.
  - should_close fires `profit_take_debit` when synthetic
    profit reaches the debit threshold.
"""
from __future__ import annotations

import pytest

from tradegy.options.chain import OptionSide
from tradegy.options.cost_model import OptionCostModel
from tradegy.options.runner import _open_position_from_order
from tradegy.options.strategies import PutCalendar30_60AtmDeb
from tradegy.options.strategy import ManagementRules, should_close


def test_calendar_produces_2leg_same_strike_diff_expiry(
    real_spx_chain_snapshots,
):
    snap = real_spx_chain_snapshots[0]
    strat = PutCalendar30_60AtmDeb()
    order = strat.on_chain(snap, open_positions=())
    assert order is not None
    assert order.tag == "put_calendar_30_60_atm_deb"
    assert len(order.legs) == 2
    # Both PUTs.
    assert all(l.side == OptionSide.PUT for l in order.legs)
    # Quantities ±1 (one short, one long).
    assert sorted(l.quantity for l in order.legs) == [-1, +1]
    # Same strike.
    strikes = [l.strike for l in order.legs]
    assert strikes[0] == strikes[1]
    # Different expiries.
    expiries = [l.expiry for l in order.legs]
    assert expiries[0] != expiries[1]


def test_calendar_short_is_front_long_is_back(real_spx_chain_snapshots):
    """The short leg should be the EARLIER-expiring (front) leg
    so we collect fast-decaying premium; long leg is the back."""
    snap = real_spx_chain_snapshots[0]
    order = PutCalendar30_60AtmDeb().on_chain(snap, ())
    assert order is not None
    short_leg = next(l for l in order.legs if l.quantity == -1)
    long_leg = next(l for l in order.legs if l.quantity == +1)
    assert short_leg.expiry < long_leg.expiry


def test_calendar_atm_strike_within_20_of_spot(real_spx_chain_snapshots):
    """ATM strike should be within $20 of spot for SPX (typical
    strike density is $5 around ATM, $25 further out)."""
    snap = real_spx_chain_snapshots[0]
    order = PutCalendar30_60AtmDeb().on_chain(snap, ())
    assert order is not None
    strike = order.legs[0].strike
    assert abs(strike - snap.underlying_price) <= 20.0, (
        f"ATM strike {strike} too far from spot {snap.underlying_price}"
    )


def test_calendar_is_debit_position(real_spx_chain_snapshots):
    """Net entry credit is NEGATIVE for a calendar (we pay
    premium); equivalently, the debit per share is positive.
    """
    snap_entry = real_spx_chain_snapshots[0]
    snap_fill = real_spx_chain_snapshots[1]
    order = PutCalendar30_60AtmDeb().on_chain(snap_entry, ())
    assert order is not None
    pos = _open_position_from_order(
        order, snap_fill, OptionCostModel(), position_id="cal",
    )
    assert pos is not None
    assert pos.entry_credit_per_share < 0, (
        f"calendar should pay net debit (negative credit); got "
        f"{pos.entry_credit_per_share}"
    )


def test_calendar_max_loss_equals_debit_paid(real_spx_chain_snapshots):
    """Max loss = debit paid for same-strike calendars (intrinsics
    cancel at every spot value, so worst-case payoff at expiration
    is zero net of premium = debit lost). Verify against
    compute_max_loss_per_contract math.
    """
    snap_entry = real_spx_chain_snapshots[0]
    snap_fill = real_spx_chain_snapshots[1]
    order = PutCalendar30_60AtmDeb().on_chain(snap_entry, ())
    pos = _open_position_from_order(
        order, snap_fill, OptionCostModel(), position_id="cal",
    )
    assert pos is not None
    debit_dollars = -pos.entry_credit_dollars
    # max_loss should match debit (within rounding).
    assert pos.max_loss_per_contract == pytest.approx(
        debit_dollars / pos.contracts, rel=0.01,
    )


def test_calendar_days_to_expiry_returns_front(real_spx_chain_snapshots):
    """The 21-DTE rule must fire on the FRONT leg, not the back.
    days_to_expiry returns the nearest expiry — verify."""
    snap_entry = real_spx_chain_snapshots[0]
    snap_fill = real_spx_chain_snapshots[1]
    order = PutCalendar30_60AtmDeb().on_chain(snap_entry, ())
    pos = _open_position_from_order(
        order, snap_fill, OptionCostModel(), position_id="cal",
    )
    assert pos is not None
    front_expiry = min(l.expiry for l in order.legs)
    expected_dte = (front_expiry - snap_fill.ts_utc.date()).days
    assert pos.days_to_expiry(snap_fill.ts_utc) == expected_dte


def test_calendar_pnl_pct_of_debit_is_real(real_spx_chain_snapshots):
    """For a debit position, pnl_pct_of_debit returns a real
    number; pnl_pct_of_max_credit returns NaN.
    """
    snap_entry = real_spx_chain_snapshots[0]
    snap_fill = real_spx_chain_snapshots[1]
    mark_index = min(3, len(real_spx_chain_snapshots) - 1)
    snap_mark = real_spx_chain_snapshots[mark_index]
    order = PutCalendar30_60AtmDeb().on_chain(snap_entry, ())
    pos = _open_position_from_order(
        order, snap_fill, OptionCostModel(), position_id="cal",
    )
    assert pos is not None
    pct_credit = pos.pnl_pct_of_max_credit(snap_mark)
    pct_debit = pos.pnl_pct_of_debit(snap_mark)
    assert pct_credit != pct_credit  # NaN for credit-side metric
    assert pct_debit == pct_debit    # real number for debit-side


def test_should_close_dispatches_to_debit_branch(real_spx_chain_snapshots):
    """When the position is a debit calendar AND ManagementRules
    has profit_take_pct_of_debit set AND the position has decayed
    enough, should_close fires `profit_take_debit`. Use a tight
    threshold to force the trigger on real data.
    """
    snap_entry = real_spx_chain_snapshots[0]
    snap_fill = real_spx_chain_snapshots[1]
    mark_index = min(3, len(real_spx_chain_snapshots) - 1)
    snap_mark = real_spx_chain_snapshots[mark_index]
    order = PutCalendar30_60AtmDeb().on_chain(snap_entry, ())
    pos = _open_position_from_order(
        order, snap_fill, OptionCostModel(), position_id="cal",
    )
    assert pos is not None

    pnl_pct = pos.pnl_pct_of_debit(snap_mark)
    # Set the profit-take threshold below the actual current pct
    # if positive, OR set the loss-stop threshold above the
    # actual current loss if negative — in either case the
    # trigger fires.
    if pnl_pct > 0:
        rules = ManagementRules(
            dte_close=1,  # very low so DTE doesn't pre-empt
            profit_take_pct_of_debit=pnl_pct * 0.5,
            loss_stop_pct_of_debit=10.0,
        )
        reason = should_close(pos, snap_mark, rules)
        assert reason is not None and "profit_take_debit" in reason
    else:
        rules = ManagementRules(
            dte_close=1,
            profit_take_pct_of_debit=10.0,
            loss_stop_pct_of_debit=abs(pnl_pct) * 0.5,
        )
        reason = should_close(pos, snap_mark, rules)
        assert reason is not None and "loss_stop_debit" in reason


def test_calendar_skips_when_position_open(real_spx_chain_snapshots):
    snap = real_spx_chain_snapshots[0]
    from tradegy.options.positions import MultiLegPosition
    fake_open = (
        MultiLegPosition(
            position_id="dummy", strategy_class="put_calendar_30_60_atm_deb",
            contracts=1, legs=(), entry_ts=snap.ts_utc,
            entry_credit_per_share=-3.0,  # debit
            max_loss_per_contract=300.0,
        ),
    )
    assert PutCalendar30_60AtmDeb().on_chain(snap, fake_open) is None
