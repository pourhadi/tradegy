"""Black-Scholes pricing + Greeks for European-style options.

Vendor-independent computation of price + delta + gamma + theta +
vega + rho + implied vol from a small set of standard inputs:

    underlying_price  (S)   spot underlying
    strike            (K)   contract strike
    time_to_expiry    (T)   year fraction (ACT/365)
    risk_free_rate    (r)   annualized continuous compounding
    volatility        (σ)   annualized sigma
    side                    OptionSide.CALL or OptionSide.PUT
    dividend_yield    (q)   annualized continuous dividend yield;
                            defaults to 0 (correct for SPX index)

Why not delegate to vendor Greeks: ORATS and CBOE both publish
Greeks but with different model choices (dividend handling, day-
count, vol-surface interpolation). Computing ourselves means
backtest reproducibility doesn't depend on the live vendor and
cross-vendor parity is decidable on our terms.

References:

- Hull, "Options, Futures, and Other Derivatives" 10th ed §15-§19.
- The closed-form Greeks here are the textbook generalized BS
  forms with continuous dividend yield (the q form). Setting q=0
  recovers the classic Black-Scholes for non-dividend-paying
  underlying; for SPX use q ≈ S&P 500 dividend yield (~1.3% as of
  2026) or 0 if the index version (SPX is total-return-aware in
  some indices but the cash SPX ignores dividends, and OPRA
  options on SPX use the cash index).

The implementation favors clarity and testability over vectorized
performance — a chain snapshot has 100-1000 legs at most, daily
cadence, so per-leg scalar Python is fine. If we need performance
later, swap in `numpy` ufuncs without changing the API.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from tradegy.options.chain import OptionSide


# Standard normal CDF + PDF. We use math.erf so this stays
# dependency-free; for vectorized use, swap in scipy.stats.norm.
def _norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """Standard normal probability density function."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


@dataclass(frozen=True)
class Greeks:
    """First-order Greeks for one option leg.

    All values are per-share (i.e., not multiplied by contract
    size). The harness multiplies by contract multiplier (100 for
    SPX, 50 for /ES) when aggregating to dollar exposure.

    Sign conventions:
      delta:  call ∈ (0, 1); put ∈ (-1, 0)
      gamma:  always > 0 (long convexity)
      theta:  always < 0 for long options (time decay loses money
              for the holder; a short option seller has +theta)
      vega:   per 1.00 (i.e., 100 vol-points) change in σ; divide
              by 100 for "per 1 vol-point" reporting
      rho:    per 1.00 (i.e., 100 bp) change in r; divide by 100
              for "per 1 bp" reporting
    """

    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float


def _d1_d2(
    S: float, K: float, T: float, r: float, sigma: float, q: float,
) -> tuple[float, float]:
    """Standard d1, d2 helpers used throughout the BS Greeks."""
    if T <= 0.0 or sigma <= 0.0:
        # Degenerate cases — caller decides whether to treat as
        # terminal value (intrinsic) or refuse to price. The price
        # / Greeks functions handle T<=0 explicitly above this.
        raise ValueError(
            f"_d1_d2 requires T > 0 and sigma > 0 "
            f"(got T={T}, sigma={sigma})"
        )
    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return d1, d2


def bs_price(
    *,
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    side: OptionSide,
    q: float = 0.0,
) -> float:
    """Black-Scholes price (per share) for a European option.

    At expiry (T <= 0) returns intrinsic value max(S-K, 0) for a
    call, max(K-S, 0) for a put. With sigma <= 0 returns the
    forward-discounted intrinsic (which for sigma=0 is the
    deterministic payoff).
    """
    if T <= 0.0:
        intrinsic = max(S - K, 0.0) if side == OptionSide.CALL else max(K - S, 0.0)
        return intrinsic
    if sigma <= 0.0:
        # Zero-vol limit: the option is deterministic given the
        # forward. Price equals discounted intrinsic at the forward.
        forward = S * math.exp((r - q) * T)
        intrinsic = (
            max(forward - K, 0.0)
            if side == OptionSide.CALL
            else max(K - forward, 0.0)
        )
        return intrinsic * math.exp(-r * T)

    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    if side == OptionSide.CALL:
        return S * disc_q * _norm_cdf(d1) - K * disc_r * _norm_cdf(d2)
    return K * disc_r * _norm_cdf(-d2) - S * disc_q * _norm_cdf(-d1)


def bs_greeks(
    *,
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    side: OptionSide,
    q: float = 0.0,
) -> Greeks:
    """Closed-form Greeks for a European option.

    See module docstring for sign + scaling conventions. Theta is
    returned per-year; divide by 365 for per-calendar-day or by
    252 for per-trading-day reporting.

    For T<=0 or sigma<=0 returns zero Greeks (caller usually
    treats these as already-expired / locked positions).
    """
    if T <= 0.0 or sigma <= 0.0:
        return Greeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0, rho=0.0)

    d1, d2 = _d1_d2(S, K, T, r, sigma, q)
    disc_r = math.exp(-r * T)
    disc_q = math.exp(-q * T)
    n_d1 = _norm_pdf(d1)
    sqrt_t = math.sqrt(T)

    # Gamma + vega are side-independent.
    gamma = (disc_q * n_d1) / (S * sigma * sqrt_t)
    vega = S * disc_q * n_d1 * sqrt_t

    if side == OptionSide.CALL:
        delta = disc_q * _norm_cdf(d1)
        theta = (
            -(S * disc_q * n_d1 * sigma) / (2.0 * sqrt_t)
            - r * K * disc_r * _norm_cdf(d2)
            + q * S * disc_q * _norm_cdf(d1)
        )
        rho = K * T * disc_r * _norm_cdf(d2)
    else:
        delta = -disc_q * _norm_cdf(-d1)
        theta = (
            -(S * disc_q * n_d1 * sigma) / (2.0 * sqrt_t)
            + r * K * disc_r * _norm_cdf(-d2)
            - q * S * disc_q * _norm_cdf(-d1)
        )
        rho = -K * T * disc_r * _norm_cdf(-d2)

    return Greeks(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho)


def implied_vol(
    *,
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    side: OptionSide,
    q: float = 0.0,
    initial_guess: float = 0.20,
    tolerance: float = 1e-7,
    max_iterations: int = 100,
) -> float:
    """Newton-Raphson implied-vol solver.

    Returns the volatility σ such that bs_price(σ) == market_price
    within `tolerance` (in dollars). Raises ValueError if the
    market price is below intrinsic (no-arbitrage violation), if T
    is non-positive (no time value to back out), or if Newton
    fails to converge in `max_iterations` (rare; usually means a
    bad input).

    The solver uses vega as the derivative; vega is well-behaved
    away from very-deep-OTM / near-expiry corners. For those
    corner cases falls back to a bisection-style fallback after
    detecting a low-vega step.
    """
    if T <= 0.0:
        raise ValueError(
            f"implied_vol requires T > 0 to extract a volatility "
            f"(got T={T})"
        )
    intrinsic = (
        max(S - K, 0.0) if side == OptionSide.CALL else max(K - S, 0.0)
    )
    intrinsic_pv = intrinsic * math.exp(-r * T)
    if market_price < intrinsic_pv - tolerance:
        raise ValueError(
            f"market price {market_price} is below intrinsic PV "
            f"{intrinsic_pv} — no-arbitrage violation, refusing to "
            "solve for IV"
        )

    sigma = max(initial_guess, 1e-4)
    for _ in range(max_iterations):
        price = bs_price(S=S, K=K, T=T, r=r, sigma=sigma, side=side, q=q)
        diff = price - market_price
        if abs(diff) < tolerance:
            return sigma
        greeks = bs_greeks(S=S, K=K, T=T, r=r, sigma=sigma, side=side, q=q)
        if greeks.vega < 1e-8:
            # Near-degenerate — bisection-step fallback toward a
            # higher sigma (most common cause: deep-OTM).
            sigma *= 2.0
            sigma = min(sigma, 5.0)
            continue
        sigma -= diff / greeks.vega
        if sigma <= 0.0:
            sigma = 1e-4
        if sigma > 5.0:
            sigma = 5.0  # 500% vol cap — anything beyond is unphysical

    raise ValueError(
        f"implied_vol failed to converge in {max_iterations} "
        f"iterations (last sigma={sigma}, price diff={diff:.4g})"
    )
