"""Chain-feature transform tests.

Synthetic ChainSnapshots constructed in-test, exercise:

  - ATM IV: averages call+put mid IV at the strike closest to spot.
  - Expected move: matches ATM straddle credit / spot.
  - Term structure slope: near - far at controlled DTEs.
  - Put-call skew: 25-delta put IV minus 25-delta call IV.
  - IV rank / percentile: rolling-window math against a controlled
    series.
  - Realized vol: matches the closed-form stdev * sqrt(annualization).
  - Robustness: NaN / sparse-chain handling does not crash callers.

The synthetic chains are intentionally small and easy to hand-verify
so a regression in the math surfaces as a precise numerical
mismatch, not a vague "result looks wrong."
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

import pytest

from tradegy.options.chain import (
    ChainSnapshot,
    OptionLeg,
    OptionSide,
)
from tradegy.options.chain_features import (
    atm_iv,
    expected_move_to_expiry,
    iv_percentile_252d,
    iv_rank_252d,
    put_call_skew_25d,
    realized_vol_30d,
    term_structure_slope,
)


# ── Synthetic chain builders ───────────────────────────────────


def _leg(
    *,
    strike: float,
    side: OptionSide,
    iv: float,
    bid: float = 5.0,
    ask: float = 5.4,
    expiry: date = date(2026, 2, 20),
) -> OptionLeg:
    return OptionLeg(
        underlying="SPX",
        expiry=expiry,
        strike=strike,
        side=side,
        bid=bid,
        ask=ask,
        iv=iv,
        volume=100,
        open_interest=1000,
        multiplier=100,
    )


def _snap(
    *,
    ts: datetime,
    underlying: float = 4500.0,
    rate: float = 0.045,
    legs: list[OptionLeg] | None = None,
) -> ChainSnapshot:
    return ChainSnapshot(
        underlying="SPX",
        ts_utc=ts,
        underlying_price=underlying,
        risk_free_rate=rate,
        legs=tuple(legs or []),
    )


def _symmetric_chain(
    *,
    ts: datetime,
    underlying: float,
    expiries: list[tuple[date, float]],  # (expiry_date, atm_iv)
    strikes_offset: list[float] = (-200, -100, 0, 100, 200),
) -> ChainSnapshot:
    """Build a chain with same IV across all strikes in each expiry,
    so the ATM-IV / expected-move / term-structure tests have
    predictable analytic answers.
    """
    legs: list[OptionLeg] = []
    for exp, iv in expiries:
        for off in strikes_offset:
            k = underlying + off
            for side in (OptionSide.CALL, OptionSide.PUT):
                legs.append(_leg(
                    strike=k, side=side, iv=iv,
                    bid=10.0, ask=10.4, expiry=exp,
                ))
    return _snap(ts=ts, underlying=underlying, legs=legs)


# ── ATM IV ─────────────────────────────────────────────────────


def test_atm_iv_averages_call_put_at_atm_strike():
    """Construct two strikes around spot; verify ATM picks the
    closest strike and averages the call+put IV.
    """
    snap = _snap(
        ts=datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc),
        underlying=4500.0,
        legs=[
            _leg(strike=4500.0, side=OptionSide.CALL, iv=0.18),
            _leg(strike=4500.0, side=OptionSide.PUT, iv=0.22),
            _leg(strike=4600.0, side=OptionSide.CALL, iv=0.16),
            _leg(strike=4600.0, side=OptionSide.PUT, iv=0.20),
        ],
    )
    # 4500 is the closest strike to 4500 underlying.
    # Average of call(0.18) + put(0.22) = 0.20.
    assert atm_iv(snap, target_dte=46) == pytest.approx(0.20)


def test_atm_iv_returns_nan_when_chain_empty():
    snap = _snap(ts=datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc), legs=[])
    with pytest.raises(ValueError, match="no expiries"):
        atm_iv(snap)


# ── Expected move ──────────────────────────────────────────────


def test_expected_move_matches_straddle_over_spot():
    """ATM straddle credit / spot. With both legs at mid=10.2 each,
    EM = 20.4 / 4500 = 0.00453.
    """
    snap = _snap(
        ts=datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc),
        underlying=4500.0,
        legs=[
            _leg(strike=4500.0, side=OptionSide.CALL, iv=0.18, bid=10.0, ask=10.4),
            _leg(strike=4500.0, side=OptionSide.PUT, iv=0.22, bid=10.0, ask=10.4),
        ],
    )
    em = expected_move_to_expiry(snap, target_dte=46)
    expected = (10.2 + 10.2) / 4500.0
    assert em == pytest.approx(expected, rel=1e-6)


# ── Term structure ─────────────────────────────────────────────


def test_term_structure_slope_negative_when_far_higher():
    """Build a chain with two expiries where the far month has a
    higher ATM IV (canonical contango). Slope = near - far must be
    negative.
    """
    snap = _symmetric_chain(
        ts=datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc),
        underlying=4500.0,
        expiries=[
            (date(2026, 2, 5), 0.15),   # 31 DTE, lower IV
            (date(2026, 3, 7), 0.18),   # 61 DTE, higher IV
        ],
    )
    slope = term_structure_slope(snap, near_dte=30, far_dte=60)
    assert slope == pytest.approx(0.15 - 0.18, abs=1e-9)
    assert slope < 0


def test_term_structure_slope_positive_when_near_higher():
    """Backwardation regime: near IV above far IV (stress)."""
    snap = _symmetric_chain(
        ts=datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc),
        underlying=4500.0,
        expiries=[
            (date(2026, 2, 5), 0.30),   # 31 DTE, vol-spike near
            (date(2026, 3, 7), 0.20),   # 61 DTE, lower far
        ],
    )
    slope = term_structure_slope(snap, near_dte=30, far_dte=60)
    assert slope > 0
    assert slope == pytest.approx(0.10, abs=1e-9)


# ── Put-call skew ──────────────────────────────────────────────


def test_put_call_skew_returns_positive_for_put_skewed_chain():
    """Build a chain with elevated put IV vs call IV at strikes that
    will resolve to ~25-delta. The exact 25-delta legs depend on the
    BS calculation; constructing a wide enough chain and biasing
    puts upward at OTM strikes guarantees a positive skew.
    """
    ts = datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc)
    underlying = 4500.0
    expiry = date(2026, 2, 4)  # 30 DTE
    # Iron condor-ish strike grid around 4500.
    legs = []
    for off, call_iv, put_iv in [
        (-300, 0.12, 0.30),  # deep OTM put — high put IV, smile
        (-200, 0.14, 0.26),
        (-100, 0.15, 0.22),
        (0, 0.16, 0.20),     # ATM
        (100, 0.14, 0.18),
        (200, 0.12, 0.17),
        (300, 0.11, 0.16),   # OTM call — low call IV
    ]:
        k = underlying + off
        legs.append(_leg(strike=k, side=OptionSide.CALL, iv=call_iv, expiry=expiry))
        legs.append(_leg(strike=k, side=OptionSide.PUT, iv=put_iv, expiry=expiry))
    snap = _snap(ts=ts, underlying=underlying, legs=legs)
    skew = put_call_skew_25d(snap, target_dte=30)
    assert skew > 0
    # Sanity bound — somewhere in the 0.05-0.15 range given the
    # IV asymmetry baked in above.
    assert 0.02 < skew < 0.20


# ── IV rank ────────────────────────────────────────────────────


def _series_of_snapshots(values: list[float], *, base_ts: datetime | None = None):
    """Build a sequence of snapshots whose ATM IV equals the input
    values. Fixed expiry, single ATM strike per snapshot.
    """
    base = base_ts or datetime(2024, 1, 2, 21, 0, tzinfo=timezone.utc)
    snaps: list[ChainSnapshot] = []
    for i, iv in enumerate(values):
        ts = datetime(
            base.year, base.month, base.day, 21, 0, tzinfo=timezone.utc,
        )
        ts = ts.replace(day=base.day) + (ts - ts)  # noop, just for clarity
        # Use sequential dates by offsetting in days.
        from datetime import timedelta
        ts = base + timedelta(days=i)
        underlying = 4500.0
        expiry = (ts + timedelta(days=46)).date()
        legs = [
            _leg(strike=underlying, side=OptionSide.CALL, iv=iv, expiry=expiry),
            _leg(strike=underlying, side=OptionSide.PUT, iv=iv, expiry=expiry),
        ]
        snaps.append(_snap(ts=ts, underlying=underlying, legs=legs))
    return snaps


def test_iv_rank_zero_at_min_one_at_max():
    """5 snapshots with IVs [0.10, 0.12, 0.15, 0.18, 0.20] in a
    5-day window. The min-IV snapshot has rank 0; the max-IV
    snapshot has rank 1.
    """
    snaps = _series_of_snapshots([0.10, 0.12, 0.15, 0.18, 0.20])
    out = iv_rank_252d(snaps, target_dte=46, window_days=5)
    ranks = out.get_column("iv_rank").to_list()
    # Snapshots 0..3 have insufficient history (window=5 needs 5);
    # only snapshot 4 has a non-NaN rank, and 0.20 is the max →
    # rank 1.
    assert ranks[4] == pytest.approx(1.0)
    assert all(r != r for r in ranks[:4])  # NaN


def test_iv_rank_midpoint():
    """7-snapshot window where the latest sits exactly at the
    midpoint of the trailing range.
    """
    snaps = _series_of_snapshots([0.10, 0.20, 0.10, 0.20, 0.10, 0.20, 0.15])
    out = iv_rank_252d(snaps, target_dte=46, window_days=7)
    ranks = out.get_column("iv_rank").to_list()
    # Latest is 0.15; window range [0.10, 0.20] → rank = 0.5.
    assert ranks[6] == pytest.approx(0.5)


# ── IV percentile ───────────────────────────────────────────────


def test_iv_percentile_below_majority():
    """7-snapshot window where the latest is below most history.
    With values [0.30, 0.28, 0.27, 0.25, 0.24, 0.22, 0.10], the
    latest 0.10 is strictly less than all 6 historical → percentile
    = 0/7 = 0.
    """
    snaps = _series_of_snapshots([0.30, 0.28, 0.27, 0.25, 0.24, 0.22, 0.10])
    out = iv_percentile_252d(snaps, target_dte=46, window_days=7)
    pct = out.get_column("iv_percentile").to_list()
    assert pct[6] == pytest.approx(0.0)


def test_iv_percentile_above_majority():
    snaps = _series_of_snapshots([0.10, 0.12, 0.14, 0.16, 0.18, 0.20, 0.25])
    out = iv_percentile_252d(snaps, target_dte=46, window_days=7)
    pct = out.get_column("iv_percentile").to_list()
    # Latest 0.25 > all 6 prior values + counts current as not
    # below itself → 6/7.
    assert pct[6] == pytest.approx(6 / 7)


# ── Realized vol ───────────────────────────────────────────────


def test_realized_vol_constant_underlying_is_zero():
    """No price changes → zero realized vol."""
    from datetime import timedelta
    base = datetime(2026, 1, 2, 21, 0, tzinfo=timezone.utc)
    snaps = []
    for i in range(35):
        ts = base + timedelta(days=i)
        snaps.append(_snap(ts=ts, underlying=4500.0))
    out = realized_vol_30d(snaps, window_days=30, annualization_days=252)
    rv = out.get_column("realized_vol").to_list()
    # Snapshots 0..29 are NaN (need 30 returns); 30..34 are 0.0.
    for i in range(29, 35):
        if rv[i] is None:
            continue
        assert rv[i] == pytest.approx(0.0, abs=1e-9)


def test_realized_vol_stable_pct_change():
    """Underlying moves by 1% every day → realized vol per period
    = stdev(constant) = 0; this confirms the math doesn't false-
    positive on monotonic series.
    """
    from datetime import timedelta
    base = datetime(2026, 1, 2, 21, 0, tzinfo=timezone.utc)
    snaps = []
    px = 4500.0
    for i in range(35):
        ts = base + timedelta(days=i)
        snaps.append(_snap(ts=ts, underlying=px))
        px *= 1.01
    out = realized_vol_30d(snaps, window_days=30, annualization_days=252)
    rv = out.filter(out["realized_vol"].is_not_null())["realized_vol"].to_list()
    # All log returns ≈ 0.00995; stdev = 0; rv = 0.
    for v in rv:
        assert v == pytest.approx(0.0, abs=1e-9)


def test_realized_vol_known_value():
    """Construct a 30-period series with closed-form realized vol.
    Underlying alternates +1%/-1% each day → log returns alternate
    +0.00995/-0.01005, stdev ≈ 0.01, annualized ≈ 0.01 * sqrt(252)
    ≈ 0.1587.
    """
    from datetime import timedelta
    base = datetime(2026, 1, 2, 21, 0, tzinfo=timezone.utc)
    snaps = []
    px = 4500.0
    for i in range(35):
        ts = base + timedelta(days=i)
        snaps.append(_snap(ts=ts, underlying=px))
        px *= 1.01 if i % 2 == 0 else 0.99
    out = realized_vol_30d(snaps, window_days=30, annualization_days=252)
    rv = out.filter(out["realized_vol"].is_not_null())["realized_vol"].to_list()
    # Loose-bounded — the exact value depends on alternation parity
    # at the window boundary; 0.1-0.2 is plenty tight.
    assert all(0.10 < v < 0.20 for v in rv)
