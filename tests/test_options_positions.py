"""Multi-leg position model tests.

Cover the load-bearing arithmetic:

  - Quantity convention: long > 0 (paid premium), short < 0
    (received).
  - cost_to_open: signed cash flow at entry per leg.
  - cost_to_close: reverses sign at current price.
  - mark_to_market: per-share unrealized P&L matches manual calc.
  - MultiLegPosition.entry_credit_dollars: net per-share credit ×
    multiplier × contracts.
  - mark_dollars: portfolio-level dollar P&L tracks individual leg
    marks correctly.
  - compute_max_loss_per_contract: matches closed-form for iron
    condor and put credit spread.
  - days_to_expiry: nearest-leg-wins for diagonals/calendars.
  - contract_id: deterministic, stable for same (underlying, expiry,
    strike, side).
  - Sparse-chain mark fallback: missing legs use intrinsic, not NaN.

Iron condor scenario used as the primary validation harness because
it exercises 4 legs with two short and two long, both calls and
puts, and has a well-known closed-form max-loss formula.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from tradegy.options.chain import (
    ChainSnapshot,
    OptionLeg,
    OptionSide,
)
from tradegy.options.positions import (
    LegOrder,
    MultiLegOrder,
    MultiLegPosition,
    OptionPosition,
    compute_max_loss_per_contract,
)


# ── Helpers ────────────────────────────────────────────────────


_ENTRY_TS = datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc)
_EXPIRY = date(2026, 2, 20)


def _opt_pos(
    *,
    strike: float,
    side: OptionSide,
    quantity: int,
    entry_price: float,
    expiry: date = _EXPIRY,
    multiplier: int = 100,
) -> OptionPosition:
    return OptionPosition(
        contract_id=OptionPosition.make_contract_id(
            "SPX", expiry, strike, side,
        ),
        underlying="SPX",
        expiry=expiry,
        strike=strike,
        side=side,
        multiplier=multiplier,
        quantity=quantity,
        entry_price=entry_price,
        entry_ts=_ENTRY_TS,
    )


def _iron_condor(
    *,
    underlying_at_entry: float = 4500.0,
    short_put: float = 4400.0,
    long_put: float = 4350.0,
    short_call: float = 4600.0,
    long_call: float = 4650.0,
    short_put_credit: float = 5.0,    # premium received per share
    long_put_debit: float = 2.0,
    short_call_credit: float = 4.0,
    long_call_debit: float = 1.5,
    contracts: int = 1,
) -> MultiLegPosition:
    """Build a synthetic iron condor with known entry credit. Net
    credit per share = (5 + 4) - (2 + 1.5) = 5.50.
    """
    legs = (
        _opt_pos(strike=long_put, side=OptionSide.PUT, quantity=+1, entry_price=long_put_debit),
        _opt_pos(strike=short_put, side=OptionSide.PUT, quantity=-1, entry_price=short_put_credit),
        _opt_pos(strike=short_call, side=OptionSide.CALL, quantity=-1, entry_price=short_call_credit),
        _opt_pos(strike=long_call, side=OptionSide.CALL, quantity=+1, entry_price=long_call_debit),
    )
    entry_credit = (short_put_credit + short_call_credit) - (long_put_debit + long_call_debit)
    max_loss = compute_max_loss_per_contract(legs, entry_credit)
    return MultiLegPosition(
        position_id="ic_test_1",
        strategy_class="iron_condor",
        contracts=contracts,
        legs=legs,
        entry_ts=_ENTRY_TS,
        entry_credit_per_share=entry_credit,
        max_loss_per_contract=max_loss,
    )


# ── Quantity convention + per-leg arithmetic ───────────────────


def test_long_leg_cost_to_open_is_positive():
    """Pay $5 for a long call → cost_to_open = +5."""
    leg = _opt_pos(strike=4500.0, side=OptionSide.CALL, quantity=+1, entry_price=5.0)
    assert leg.cost_to_open() == pytest.approx(5.0)


def test_short_leg_cost_to_open_is_negative():
    """Receive $3 for a short put → cost_to_open = -3."""
    leg = _opt_pos(strike=4400.0, side=OptionSide.PUT, quantity=-1, entry_price=3.0)
    assert leg.cost_to_open() == pytest.approx(-3.0)


def test_short_leg_mark_when_iv_crushes():
    """Sold a put for $5, now worth $2 → unrealized profit = +3 per share."""
    leg = _opt_pos(strike=4400.0, side=OptionSide.PUT, quantity=-1, entry_price=5.0)
    assert leg.mark_to_market(2.0) == pytest.approx(3.0)


def test_short_leg_mark_when_underlying_moves_against_us():
    """Sold a put for $5, now worth $8 (underlying dropped) → loss = -3 per share."""
    leg = _opt_pos(strike=4400.0, side=OptionSide.PUT, quantity=-1, entry_price=5.0)
    assert leg.mark_to_market(8.0) == pytest.approx(-3.0)


def test_long_leg_mark_when_underlying_moves_with_us():
    """Bought a call for $5, now worth $9 → profit = +4 per share."""
    leg = _opt_pos(strike=4500.0, side=OptionSide.CALL, quantity=+1, entry_price=5.0)
    assert leg.mark_to_market(9.0) == pytest.approx(4.0)


def test_contract_id_deterministic():
    a = OptionPosition.make_contract_id("SPX", _EXPIRY, 4500.0, OptionSide.CALL)
    b = OptionPosition.make_contract_id("SPX", _EXPIRY, 4500.0, OptionSide.CALL)
    assert a == b == "SPX_20260220_4500.0_C"


def test_contract_id_distinguishes_side():
    c = OptionPosition.make_contract_id("SPX", _EXPIRY, 4500.0, OptionSide.CALL)
    p = OptionPosition.make_contract_id("SPX", _EXPIRY, 4500.0, OptionSide.PUT)
    assert c != p


# ── Multi-leg arithmetic ───────────────────────────────────────


def test_iron_condor_entry_credit_dollars():
    """1-lot iron condor with $5.50 net credit per share, 100x
    multiplier → $550 in dollars at open.
    """
    pos = _iron_condor(contracts=1)
    assert pos.entry_credit_per_share == pytest.approx(5.50)
    assert pos.entry_credit_dollars == pytest.approx(550.0)


def test_iron_condor_entry_credit_scales_with_contracts():
    """5-lot iron condor → $2,750 at open."""
    pos = _iron_condor(contracts=5)
    assert pos.entry_credit_dollars == pytest.approx(2750.0)


def test_iron_condor_max_loss_matches_closed_form():
    """For an iron condor with wing width W and credit C:
        max_loss = (W - C) * multiplier
    Symmetric wings: $50 wide on each side.  Credit $5.50 → max
    loss = (50 - 5.50) * 100 = $4,450 per contract.
    """
    pos = _iron_condor()
    assert pos.max_loss_per_contract == pytest.approx(4450.0)


def test_iron_condor_max_loss_scales_with_contracts():
    pos = _iron_condor(contracts=3)
    assert pos.total_capital_at_risk == pytest.approx(13350.0)


def test_put_credit_spread_max_loss_closed_form():
    """Sell a $50-wide put spread for $1.50 credit → max loss
    = ($50 - $1.50) * $100 = $4,850 per contract.
    """
    legs = [
        _opt_pos(strike=4350.0, side=OptionSide.PUT, quantity=+1, entry_price=2.0),
        _opt_pos(strike=4400.0, side=OptionSide.PUT, quantity=-1, entry_price=3.5),
    ]
    credit = 3.5 - 2.0
    max_loss = compute_max_loss_per_contract(legs, credit)
    assert max_loss == pytest.approx(4850.0)


# ── Mark-to-market against a chain snapshot ────────────────────


def _snap_with_legs(
    *,
    underlying: float,
    short_put_price: float,
    long_put_price: float,
    short_call_price: float,
    long_call_price: float,
) -> ChainSnapshot:
    """Build a chain snapshot containing all 4 condor strikes with
    bid/ask centered on a target mid (mid = 0.5 * (bid + ask)).
    """
    def _leg(strike, side, mid):
        return OptionLeg(
            underlying="SPX", expiry=_EXPIRY, strike=strike, side=side,
            bid=mid - 0.05, ask=mid + 0.05,
            iv=0.20, volume=10, open_interest=100, multiplier=100,
        )
    legs = (
        _leg(4350.0, OptionSide.PUT, long_put_price),
        _leg(4400.0, OptionSide.PUT, short_put_price),
        _leg(4600.0, OptionSide.CALL, short_call_price),
        _leg(4650.0, OptionSide.CALL, long_call_price),
    )
    return ChainSnapshot(
        underlying="SPX",
        ts_utc=datetime(2026, 1, 12, 21, 0, tzinfo=timezone.utc),
        underlying_price=underlying,
        risk_free_rate=0.045,
        legs=legs,
    )


def test_mark_to_market_full_credit_decay():
    """Iron condor entered at $5.50 credit; underlying now at 4500
    (still in body), all options decayed to $0.50 → unrealized
    profit. Per share:
      long put leg: paid 2, now worth 0.5 → mark = -1.5
      short put leg: received 5, now worth 0.5 → mark = +4.5
      short call leg: received 4, now worth 0.5 → mark = +3.5
      long call leg: paid 1.5, now worth 0.5 → mark = -1.0
      total per share = +5.5 (we'd capture full credit)
    """
    pos = _iron_condor()
    snap = _snap_with_legs(
        underlying=4500.0,
        long_put_price=0.5, short_put_price=0.5,
        short_call_price=0.5, long_call_price=0.5,
    )
    mark = pos.mark_to_market(snap)
    assert mark == pytest.approx(5.5, rel=1e-3)


def test_mark_to_market_dollars():
    """Same scenario, 5 contracts, 100x multiplier → ~$2,750 P&L."""
    pos = _iron_condor(contracts=5)
    snap = _snap_with_legs(
        underlying=4500.0,
        long_put_price=0.5, short_put_price=0.5,
        short_call_price=0.5, long_call_price=0.5,
    )
    assert pos.mark_dollars(snap) == pytest.approx(2750.0, rel=1e-3)


def test_pnl_pct_of_max_credit_at_50pct_decay():
    """When unrealized = 50% of entry credit, the management
    trigger fires (close-at-50%-profit). Construct a scenario at
    exactly half credit captured.
    """
    pos = _iron_condor()
    # Halve the leg prices vs entry: short legs entered at 5+4=9
    # received; if they're now at 5 (worth half closing cost), the
    # short side has captured half. Long legs entered at 2+1.5=3.5
    # paid; if they're now at 1.75, the long side has lost half.
    # Net: short captures (9-5)/9 = ~44%, long loses (3.5-1.75)/3.5
    # = 50%. Combined per-share unrealized:
    #   short_put: -1 * (5 - 5) = 0... hmm not quite 50%.
    # Let me just compute a scenario that gives exactly 50% pct.
    # If all leg prices halve uniformly: long_put 1.0, short_put 2.5,
    # short_call 2.0, long_call 0.75. Then per-share:
    #   long_put leg: paid 2, now worth 1 → mark -1
    #   short_put: received 5, now worth 2.5 → mark +2.5
    #   short_call: received 4, now worth 2 → mark +2
    #   long_call: paid 1.5, now worth 0.75 → mark -0.75
    # total = -1 + 2.5 + 2 - 0.75 = +2.75
    # entry credit = 5.50 → pct = 2.75/5.50 = 0.50.
    snap = _snap_with_legs(
        underlying=4500.0,
        long_put_price=1.0, short_put_price=2.5,
        short_call_price=2.0, long_call_price=0.75,
    )
    pct = pos.pnl_pct_of_max_credit(snap)
    assert pct == pytest.approx(0.50, abs=1e-6)


# ── Days-to-expiry + multi-expiry positions ────────────────────


def test_days_to_expiry_uses_nearest():
    """Calendar / diagonal: the 21 DTE rule fires on whichever leg
    expires first.
    """
    near = date(2026, 2, 6)
    far = date(2026, 3, 6)
    legs = (
        _opt_pos(strike=4500.0, side=OptionSide.PUT, quantity=-1, entry_price=5.0, expiry=near),
        _opt_pos(strike=4500.0, side=OptionSide.PUT, quantity=+1, entry_price=10.0, expiry=far),
    )
    pos = MultiLegPosition(
        position_id="cal_1",
        strategy_class="calendar",
        contracts=1,
        legs=legs,
        entry_ts=_ENTRY_TS,
        entry_credit_per_share=-5.0,  # debit
        max_loss_per_contract=500.0,
    )
    # ts at 2026-01-25 → near is 12 days away, far is 40
    ts = datetime(2026, 1, 25, 21, 0, tzinfo=timezone.utc)
    assert pos.days_to_expiry(ts) == 12


# ── Sparse-chain fallback ──────────────────────────────────────


def test_mark_to_market_falls_back_to_intrinsic_for_missing_leg():
    """Snapshot doesn't contain one of the strikes (sparse chain).
    Mark falls back to intrinsic value at snapshot underlying.
    Sanity check: the result is a real number, not NaN.
    """
    pos = _iron_condor()
    # Build a snapshot that includes only TWO of the four legs.
    legs = (
        OptionLeg(
            underlying="SPX", expiry=_EXPIRY, strike=4400.0,
            side=OptionSide.PUT, bid=2.0, ask=2.1, iv=0.20,
            volume=10, open_interest=100, multiplier=100,
        ),
        OptionLeg(
            underlying="SPX", expiry=_EXPIRY, strike=4600.0,
            side=OptionSide.CALL, bid=1.5, ask=1.6, iv=0.20,
            volume=10, open_interest=100, multiplier=100,
        ),
    )
    snap = ChainSnapshot(
        underlying="SPX",
        ts_utc=datetime(2026, 1, 19, 21, 0, tzinfo=timezone.utc),
        underlying_price=4500.0,
        risk_free_rate=0.045,
        legs=legs,
    )
    mark = pos.mark_to_market(snap)
    assert mark == mark  # not NaN


# ── MultiLegOrder dataclass ────────────────────────────────────


def test_multi_leg_order_construction():
    order = MultiLegOrder(
        tag="iron_condor_45dte_d16",
        contracts=2,
        legs=(
            LegOrder(expiry=_EXPIRY, strike=4350.0, side=OptionSide.PUT, quantity=+1),
            LegOrder(expiry=_EXPIRY, strike=4400.0, side=OptionSide.PUT, quantity=-1),
            LegOrder(expiry=_EXPIRY, strike=4600.0, side=OptionSide.CALL, quantity=-1),
            LegOrder(expiry=_EXPIRY, strike=4650.0, side=OptionSide.CALL, quantity=+1),
        ),
    )
    assert order.tag == "iron_condor_45dte_d16"
    assert order.contracts == 2
    assert len(order.legs) == 4
