"""OptionCostModel tests.

Cover:

  - Long-leg fill at mid + offset, short-leg at mid - offset.
  - One-sided quote falls back to leg.mid (which itself falls back
    to the available side).
  - Both-sides-zero returns 0.0 (caller filters dead quotes).
  - Per-leg commission scales with n_legs and contracts.
  - Round-trip commission = 2x per-side.
"""
from __future__ import annotations

from datetime import date

import pytest

from tradegy.options.chain import OptionLeg, OptionSide
from tradegy.options.cost_model import OptionCostModel


def _leg(*, bid: float = 5.00, ask: float = 5.20) -> OptionLeg:
    return OptionLeg(
        underlying="SPX",
        expiry=date(2026, 2, 20),
        strike=4500.0,
        side=OptionSide.PUT,
        bid=bid,
        ask=ask,
        iv=0.20,
        volume=10,
        open_interest=100,
        multiplier=100,
    )


# ── Fill semantics ─────────────────────────────────────────────


def test_long_fills_above_mid():
    """Long order pays slightly above mid. With bid=5.00, ask=5.20:
    mid = 5.10, half_spread = 0.10, offset_fraction = 0.20 → offset
    = 0.02. Long fill = 5.10 + 0.02 = 5.12.
    """
    cm = OptionCostModel(spread_offset_fraction=0.20)
    leg = _leg(bid=5.00, ask=5.20)
    assert cm.fill_price(leg, signed_quantity=+1) == pytest.approx(5.12)


def test_short_fills_below_mid():
    """Short order receives slightly below mid → 5.10 - 0.02 = 5.08."""
    cm = OptionCostModel(spread_offset_fraction=0.20)
    leg = _leg(bid=5.00, ask=5.20)
    assert cm.fill_price(leg, signed_quantity=-1) == pytest.approx(5.08)


def test_zero_offset_means_exact_mid():
    """spread_offset_fraction=0 fills both sides at exact mid
    (optimistic, matches a "fill or kill at mid" assumption)."""
    cm = OptionCostModel(spread_offset_fraction=0.0)
    leg = _leg(bid=5.00, ask=5.20)
    assert cm.fill_price(leg, signed_quantity=+1) == pytest.approx(5.10)
    assert cm.fill_price(leg, signed_quantity=-1) == pytest.approx(5.10)


def test_offset_fraction_one_fills_at_far_side():
    """offset_fraction=1 means worst-case: long pays ask, short
    receives bid — full bid-ask spread eaten.
    """
    cm = OptionCostModel(spread_offset_fraction=1.0)
    leg = _leg(bid=5.00, ask=5.20)
    assert cm.fill_price(leg, signed_quantity=+1) == pytest.approx(5.20)
    assert cm.fill_price(leg, signed_quantity=-1) == pytest.approx(5.00)


def test_one_sided_quote_falls_back_to_mid():
    """When ask is missing, leg.mid returns bid; the cost model
    uses that without offset (we have no spread to compute).
    """
    cm = OptionCostModel(spread_offset_fraction=0.20)
    leg = _leg(bid=5.00, ask=0.0)
    assert cm.fill_price(leg, signed_quantity=+1) == 5.00


def test_both_sides_zero_returns_zero():
    """Dead quote — fill_price returns 0.0. Caller filters."""
    cm = OptionCostModel()
    leg = _leg(bid=0.0, ask=0.0)
    assert cm.fill_price(leg, signed_quantity=+1) == 0.0


# ── Commission ─────────────────────────────────────────────────


def test_commission_for_legs_default():
    """4-leg iron condor at default $0.65/leg = $2.60 to open."""
    cm = OptionCostModel()
    assert cm.commission_for_legs(4) == pytest.approx(2.60)


def test_commission_for_legs_scales_with_contracts():
    """5-lot iron condor (4 legs × 5 contracts) at $0.65/leg/contract
    = $13.00 to open.
    """
    cm = OptionCostModel()
    assert cm.commission_for_legs(4, contracts=5) == pytest.approx(13.00)


def test_round_trip_commission_doubles_open_cost():
    """Round-trip is open + close — symmetric."""
    cm = OptionCostModel()
    open_cost = cm.commission_for_legs(4)
    rt = cm.round_trip_commission(4)
    assert rt == pytest.approx(2.0 * open_cost)


def test_custom_commission_per_leg():
    cm = OptionCostModel(commission_per_leg=1.00)
    assert cm.commission_for_legs(4) == pytest.approx(4.00)
