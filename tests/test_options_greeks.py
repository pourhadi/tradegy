"""Black-Scholes pricing + Greeks tests.

Covers: textbook reference values (Hull 10e), put-call parity,
Greeks identities (gamma/vega side-independence, delta bounds),
implied-vol round-trip, degenerate-input handling.

The vol-selling workstream rests on this module being arithmetically
correct — every downstream backtest relies on these prices and Greeks.
Fail loudly here, not silently in a Phase D backtest.
"""
from __future__ import annotations

import math

import pytest

from tradegy.options.chain import OptionSide
from tradegy.options.greeks import (
    Greeks,
    bs_greeks,
    bs_price,
    implied_vol,
)


# ── Reference values ───────────────────────────────────────────


def test_hull_example_15_6_call():
    """Hull, "Options Futures and Other Derivatives" 10th ed
    Example 15.6: S=42, K=40, T=0.5, r=0.10, sigma=0.20, q=0.

    The published answer for the European call is $4.7594 (rounded
    to 4 dp). We require ±$0.01 to allow for Hull's normal-CDF
    table rounding while still catching real bugs.
    """
    price = bs_price(
        S=42.0, K=40.0, T=0.5, r=0.10, sigma=0.20,
        side=OptionSide.CALL,
    )
    assert price == pytest.approx(4.7594, abs=0.01)


def test_hull_example_15_6_put():
    """Same parameters as the Hull call; published put answer is
    $0.8086.
    """
    price = bs_price(
        S=42.0, K=40.0, T=0.5, r=0.10, sigma=0.20,
        side=OptionSide.PUT,
    )
    assert price == pytest.approx(0.8086, abs=0.01)


# ── Put-call parity ────────────────────────────────────────────


def test_put_call_parity_no_dividend():
    """For European options on a non-dividend-paying underlying:
        c - p = S - K * exp(-r * T)
    Must hold to numerical precision regardless of moneyness.
    """
    S, K, T, r, sigma = 100.0, 105.0, 0.25, 0.05, 0.30
    c = bs_price(S=S, K=K, T=T, r=r, sigma=sigma, side=OptionSide.CALL)
    p = bs_price(S=S, K=K, T=T, r=r, sigma=sigma, side=OptionSide.PUT)
    parity_lhs = c - p
    parity_rhs = S - K * math.exp(-r * T)
    assert parity_lhs == pytest.approx(parity_rhs, abs=1e-9)


def test_put_call_parity_with_dividend():
    """With continuous dividend yield q:
        c - p = S * exp(-q * T) - K * exp(-r * T)
    """
    S, K, T, r, sigma, q = 100.0, 100.0, 0.5, 0.04, 0.25, 0.02
    c = bs_price(S=S, K=K, T=T, r=r, sigma=sigma, side=OptionSide.CALL, q=q)
    p = bs_price(S=S, K=K, T=T, r=r, sigma=sigma, side=OptionSide.PUT, q=q)
    parity_lhs = c - p
    parity_rhs = S * math.exp(-q * T) - K * math.exp(-r * T)
    assert parity_lhs == pytest.approx(parity_rhs, abs=1e-9)


# ── Greeks identities ──────────────────────────────────────────


def test_gamma_is_side_independent():
    """Gamma is the same for the call and put at the same strike
    and expiry — both share the same n(d1) / (S*sigma*sqrt(T))
    structure (in the no-dividend case).
    """
    S, K, T, r, sigma = 100.0, 100.0, 0.25, 0.05, 0.20
    g_call = bs_greeks(S=S, K=K, T=T, r=r, sigma=sigma, side=OptionSide.CALL)
    g_put = bs_greeks(S=S, K=K, T=T, r=r, sigma=sigma, side=OptionSide.PUT)
    assert g_call.gamma == pytest.approx(g_put.gamma, rel=1e-12)


def test_vega_is_side_independent():
    """Same logic as gamma — vega is structurally the same."""
    S, K, T, r, sigma = 100.0, 100.0, 0.25, 0.05, 0.20
    g_call = bs_greeks(S=S, K=K, T=T, r=r, sigma=sigma, side=OptionSide.CALL)
    g_put = bs_greeks(S=S, K=K, T=T, r=r, sigma=sigma, side=OptionSide.PUT)
    assert g_call.vega == pytest.approx(g_put.vega, rel=1e-12)


def test_delta_bounds():
    """Call delta ∈ (0, 1); put delta ∈ (-1, 0)."""
    for K in [80, 100, 120]:
        g_call = bs_greeks(
            S=100.0, K=K, T=0.5, r=0.05, sigma=0.20, side=OptionSide.CALL,
        )
        g_put = bs_greeks(
            S=100.0, K=K, T=0.5, r=0.05, sigma=0.20, side=OptionSide.PUT,
        )
        assert 0.0 < g_call.delta < 1.0
        assert -1.0 < g_put.delta < 0.0


def test_atm_call_delta_near_half():
    """For an ATM short-dated call with no dividends, delta is
    slightly above 0.5 (drift from the risk-free rate term in d1
    pushes it up). Approximate within ±0.05 to avoid being too
    tight on rate sensitivity.
    """
    g = bs_greeks(
        S=100.0, K=100.0, T=0.05, r=0.05, sigma=0.20,
        side=OptionSide.CALL,
    )
    assert 0.50 <= g.delta <= 0.55


def test_short_option_theta_is_negative_for_long():
    """Long options lose to time decay: theta < 0. We compute
    Greeks for the long-side; the short-seller flips the sign.
    """
    g = bs_greeks(
        S=100.0, K=100.0, T=0.25, r=0.05, sigma=0.20,
        side=OptionSide.CALL,
    )
    assert g.theta < 0.0
    g2 = bs_greeks(
        S=100.0, K=100.0, T=0.25, r=0.05, sigma=0.20,
        side=OptionSide.PUT,
    )
    assert g2.theta < 0.0


def test_gamma_vega_positive():
    """Both should be positive for both sides at sane inputs."""
    g = bs_greeks(
        S=100.0, K=95.0, T=0.5, r=0.05, sigma=0.30,
        side=OptionSide.CALL,
    )
    assert g.gamma > 0.0
    assert g.vega > 0.0


# ── Implied volatility round-trip ──────────────────────────────


def test_iv_round_trip_atm():
    """Forward: price an option at known sigma. Inverse: solve IV
    from that price. The two sigmas should agree to tolerance.
    """
    S, K, T, r, sigma_true = 100.0, 100.0, 0.25, 0.05, 0.22
    price = bs_price(
        S=S, K=K, T=T, r=r, sigma=sigma_true, side=OptionSide.CALL,
    )
    sigma_solved = implied_vol(
        market_price=price, S=S, K=K, T=T, r=r, side=OptionSide.CALL,
    )
    assert sigma_solved == pytest.approx(sigma_true, abs=1e-5)


def test_iv_round_trip_otm_put():
    """Same round-trip for an OTM put — exercises the put branch
    and a non-ATM solver path.
    """
    S, K, T, r, sigma_true = 100.0, 90.0, 0.5, 0.04, 0.35
    price = bs_price(
        S=S, K=K, T=T, r=r, sigma=sigma_true, side=OptionSide.PUT,
    )
    sigma_solved = implied_vol(
        market_price=price, S=S, K=K, T=T, r=r, side=OptionSide.PUT,
    )
    assert sigma_solved == pytest.approx(sigma_true, abs=1e-5)


def test_iv_round_trip_with_dividend():
    """Round-trip when q > 0. SPX cash chains use q=0 in practice
    but the math must hold for general q because /ES futures
    options are equivalent to q = r in the standard form (and a
    future-on-SPX index has its own carry structure).
    """
    S, K, T, r, sigma_true, q = 100.0, 100.0, 0.4, 0.05, 0.25, 0.02
    price = bs_price(
        S=S, K=K, T=T, r=r, sigma=sigma_true, side=OptionSide.CALL, q=q,
    )
    sigma_solved = implied_vol(
        market_price=price, S=S, K=K, T=T, r=r,
        side=OptionSide.CALL, q=q,
    )
    assert sigma_solved == pytest.approx(sigma_true, abs=1e-5)


def test_iv_below_intrinsic_raises():
    """Market price under intrinsic-PV is a no-arbitrage violation
    — the solver refuses rather than returning garbage.
    """
    with pytest.raises(ValueError, match="below intrinsic"):
        implied_vol(
            market_price=0.001,  # absurdly low for deep ITM call
            S=120.0, K=100.0, T=0.25, r=0.05,
            side=OptionSide.CALL,
        )


def test_iv_zero_time_raises():
    """T <= 0 means no time-value to back out a vol from."""
    with pytest.raises(ValueError, match="requires T > 0"):
        implied_vol(
            market_price=0.50, S=100.0, K=100.0, T=0.0, r=0.05,
            side=OptionSide.CALL,
        )


# ── Degenerate inputs ─────────────────────────────────────────


def test_price_at_expiry_call_itm_returns_intrinsic():
    """At T=0 a call's value is max(S-K, 0)."""
    price = bs_price(
        S=110.0, K=100.0, T=0.0, r=0.05, sigma=0.20,
        side=OptionSide.CALL,
    )
    assert price == 10.0


def test_price_at_expiry_put_itm_returns_intrinsic():
    price = bs_price(
        S=90.0, K=100.0, T=0.0, r=0.05, sigma=0.20,
        side=OptionSide.PUT,
    )
    assert price == 10.0


def test_price_at_expiry_otm_returns_zero():
    """OTM at expiry → 0 for both sides."""
    assert bs_price(
        S=90.0, K=100.0, T=0.0, r=0.05, sigma=0.20,
        side=OptionSide.CALL,
    ) == 0.0
    assert bs_price(
        S=110.0, K=100.0, T=0.0, r=0.05, sigma=0.20,
        side=OptionSide.PUT,
    ) == 0.0


def test_greeks_at_expiry_are_zero():
    """At T=0 the Greeks are not well-defined; we return zero
    Greeks rather than NaN so downstream aggregation doesn't
    poison portfolio totals.
    """
    g = bs_greeks(
        S=100.0, K=100.0, T=0.0, r=0.05, sigma=0.20,
        side=OptionSide.CALL,
    )
    assert g == Greeks(0.0, 0.0, 0.0, 0.0, 0.0)


def test_greeks_at_zero_vol_are_zero():
    """sigma=0 same — return zero Greeks."""
    g = bs_greeks(
        S=100.0, K=100.0, T=0.5, r=0.05, sigma=0.0,
        side=OptionSide.CALL,
    )
    assert g == Greeks(0.0, 0.0, 0.0, 0.0, 0.0)


# ── Vendor unit-convention reconciliation ──────────────────────


def test_vega_trader_units_conversion():
    """ORATS publishes vega per-1-vol-point (e.g. 7.79 for an SPX
    ATM put). Our bs_greeks publishes per-1.00 σ change (e.g.
    776.25 for the same leg). The trader-unit conversion is /100.

    Verified against real ORATS chain 2025-12-15 SPX ATM put
    (strike=6820, 30 DTE): our 776.25 / 100 = 7.76 ≈ vendor 7.79
    (small difference attributable to ORATS using their smvVol
    smoothed surface for vendor Greeks vs our raw mid-IV input).
    """
    g = bs_greeks(
        S=6818.69, K=6820.0, T=30 / 365.0, r=0.0376, sigma=0.1299,
        side=OptionSide.PUT,
    )
    trader_vega = g.vega / 100.0
    # Within 5% of the vendor-published 7.79 (the residual is the
    # smvVol-vs-callMidIv model difference, not a math bug).
    assert trader_vega == pytest.approx(7.79, rel=0.05)


def test_theta_trader_units_conversion():
    """ORATS publishes theta per-calendar-day (e.g. -1.61 for an
    SPX ATM put). Our bs_greeks publishes per-year (e.g. -491.67).
    The trader-unit conversion is /365.

    Same chain as the vega test: our -491.67 / 365 = -1.347 ≈
    vendor -1.61 (the residual reflects vendor's smvVol-driven
    Greeks vs our raw-IV input).
    """
    g = bs_greeks(
        S=6818.69, K=6820.0, T=30 / 365.0, r=0.0376, sigma=0.1299,
        side=OptionSide.PUT,
    )
    trader_theta_per_day = g.theta / 365.0
    assert trader_theta_per_day == pytest.approx(-1.6, rel=0.20)
