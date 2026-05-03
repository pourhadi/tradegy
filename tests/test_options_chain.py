"""Options chain dataclass tests.

The chain module is the data shape upstream of every vol-selling
strategy — get this wrong and every backtest is wrong. Tests cover
mid-price computation (including locked / one-sided quote edge
cases), per-expiry view ordering, and dataclass immutability.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from tradegy.options.chain import (
    ChainSnapshot,
    OptionLeg,
    OptionSide,
)


# ── OptionLeg.mid ───────────────────────────────────────────────


def test_mid_normal_quote():
    leg = OptionLeg(
        underlying="SPX", expiry=date(2026, 6, 19), strike=4500.0,
        side=OptionSide.PUT,
        bid=12.50, ask=12.70, iv=0.18, volume=100, open_interest=5000,
    )
    assert leg.mid == pytest.approx(12.60)


def test_mid_falls_back_to_ask_when_bid_zero():
    """One-sided quote — bidder absent. Caller still wants a
    fillable mid for sizing purposes.
    """
    leg = OptionLeg(
        underlying="SPX", expiry=date(2026, 6, 19), strike=4500.0,
        side=OptionSide.PUT,
        bid=0.0, ask=12.70, iv=0.18, volume=0, open_interest=5000,
    )
    assert leg.mid == 12.70


def test_mid_falls_back_to_bid_when_ask_zero():
    leg = OptionLeg(
        underlying="SPX", expiry=date(2026, 6, 19), strike=4500.0,
        side=OptionSide.PUT,
        bid=12.50, ask=0.0, iv=0.18, volume=0, open_interest=5000,
    )
    assert leg.mid == 12.50


def test_mid_zero_when_both_sides_zero():
    leg = OptionLeg(
        underlying="SPX", expiry=date(2026, 6, 19), strike=4500.0,
        side=OptionSide.PUT,
        bid=0.0, ask=0.0, iv=0.18, volume=0, open_interest=0,
    )
    assert leg.mid == 0.0


def test_leg_is_immutable():
    """Frozen dataclass — no in-place mutation allowed."""
    leg = OptionLeg(
        underlying="SPX", expiry=date(2026, 6, 19), strike=4500.0,
        side=OptionSide.PUT,
        bid=12.50, ask=12.70, iv=0.18, volume=100, open_interest=5000,
    )
    with pytest.raises(Exception):
        leg.bid = 13.0  # type: ignore[misc]


# ── ChainSnapshot views ────────────────────────────────────────


def _sample_snapshot() -> ChainSnapshot:
    legs = []
    for expiry in [date(2026, 6, 19), date(2026, 7, 17)]:
        for strike in [4400.0, 4500.0, 4600.0]:
            for side in [OptionSide.CALL, OptionSide.PUT]:
                legs.append(OptionLeg(
                    underlying="SPX", expiry=expiry, strike=strike,
                    side=side,
                    bid=10.0, ask=11.0, iv=0.18,
                    volume=100, open_interest=1000,
                ))
    return ChainSnapshot(
        underlying="SPX",
        ts_utc=datetime(2026, 5, 2, 20, 0, tzinfo=timezone.utc),
        underlying_price=4500.0,
        risk_free_rate=0.045,
        legs=tuple(legs),
    )


def test_expiries_returns_unique_sorted_near_to_far():
    snap = _sample_snapshot()
    out = snap.expiries()
    assert out == (date(2026, 6, 19), date(2026, 7, 17))


def test_for_expiry_returns_only_that_expiry():
    snap = _sample_snapshot()
    legs = snap.for_expiry(date(2026, 6, 19))
    assert all(leg.expiry == date(2026, 6, 19) for leg in legs)
    assert len(legs) == 6  # 3 strikes × 2 sides


def test_for_expiry_sorted_by_strike_then_side():
    snap = _sample_snapshot()
    legs = snap.for_expiry(date(2026, 6, 19))
    keys = [(leg.strike, leg.side.value) for leg in legs]
    assert keys == sorted(keys)


def test_for_expiry_unknown_returns_empty():
    snap = _sample_snapshot()
    legs = snap.for_expiry(date(2099, 1, 1))
    assert legs == ()
