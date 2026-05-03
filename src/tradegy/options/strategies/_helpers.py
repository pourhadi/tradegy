"""Shared leg-selection helpers across vol-selling strategy classes.

Module-level (not class-based) so strategies can compose them
without inheriting a base class. The three primitives in this
module are the smallest units that every concrete strategy
needs to identify legs by DTE and delta.
"""
from __future__ import annotations

from datetime import date

from tradegy.options.chain import ChainSnapshot, OptionLeg
from tradegy.options.greeks import bs_greeks


def pick_expiry_closest_to_dte(
    snapshot: ChainSnapshot, target_dte: int,
) -> date | None:
    """Return the snapshot's expiry whose DTE is closest to
    `target_dte`. Tie-break: prefer the LATER expiry (more
    management headroom — a 21 DTE manage rule fires later on the
    later expiry).
    """
    snap_date = snapshot.ts_utc.date()
    expiries = snapshot.expiries()
    if not expiries:
        return None
    best = expiries[0]
    best_dist = abs((best - snap_date).days - target_dte)
    for e in expiries[1:]:
        d = (e - snap_date).days
        dist = abs(d - target_dte)
        if dist < best_dist or (dist == best_dist and e > best):
            best = e
            best_dist = dist
    return best


def is_fillable(
    leg: OptionLeg,
    *,
    max_spread_pct_of_mid: float = 0.50,
    min_iv: float = 0.01,
    max_iv: float = 5.0,
) -> bool:
    """Filter out dead-quote / illiquid legs at strategy-side leg
    selection.

    A leg is fillable iff:
      - bid > 0 AND ask > 0 (both sides quoted; one-sided quotes
        produce unfillable strikes that contaminate delta-target
        searches with ITM-leg false positives)
      - ask > bid (no locked / inverted quotes)
      - vendor IV in [min_iv, max_iv] (sane sigma; iv=0 means
        ORATS couldn't compute IV from the quote → typically
        sparse-chain illiquid strike that misleads bs_greeks
        delta calculations toward 0 or near-1)
      - bid-ask spread < `max_spread_pct_of_mid` of mid price
        (rejects totally-illiquid strikes — discovered 2026-05-03
        on XSP where 29%-spread strikes were getting picked as
        "0-delta" legs because their IV was junk; SPX never
        tripped this because SPX chains are dense + tight)

    Strategies that want a more permissive filter for stress
    regimes can wrap this and override.
    """
    if leg.bid <= 0.0 or leg.ask <= 0.0 or leg.ask <= leg.bid:
        return False
    if leg.iv < min_iv or leg.iv > max_iv:
        return False
    mid = leg.mid
    if mid <= 0:
        return False
    spread_pct = (leg.ask - leg.bid) / mid
    return spread_pct < max_spread_pct_of_mid


def closest_delta(
    candidates: list[OptionLeg], *,
    target: float,
    S: float, T: float, r: float,
    max_delta_tolerance: float = 0.15,
) -> OptionLeg | None:
    """Return the leg whose Black-Scholes delta is closest to
    `target`. Sign convention: call delta > 0, put delta < 0;
    pass signed target (+0.16 for 16-delta call, -0.16 for
    16-delta put).

    `max_delta_tolerance` REJECTS the closest leg if its delta is
    further than this from `target`. Defaults to 0.15 — for a
    target of 0.30, only legs with delta in [0.15, 0.45] qualify.
    Discovered 2026-05-03 on XSP: when liquid OTM strikes were
    all filtered out as illiquid, the closest_delta scan returned
    deep-ITM legs as "30-delta candidates" (their actual delta
    being ~0.70+) — produced absurd put-credit-spread structures
    with the short strike ABOVE spot. Without the tolerance, the
    function silently substitutes the wrong leg; with it, the
    strategy gets None and refuses to enter on illiquid days.

    Empty candidates → None.
    Closest-delta-out-of-tolerance → None.
    """
    if not candidates:
        return None
    best = candidates[0]
    best_diff = float("inf")
    for leg in candidates:
        g = bs_greeks(
            S=S, K=leg.strike, T=T, r=r, sigma=leg.iv, side=leg.side,
        )
        diff = abs(g.delta - target)
        if diff < best_diff:
            best = leg
            best_diff = diff
    if best_diff > max_delta_tolerance:
        return None
    return best


def closest_strike_at_offset(
    candidates: list[OptionLeg], *,
    body_strike: float,
    offset_dollars: float,
    direction: int,
) -> OptionLeg | None:
    """Return the leg whose strike is closest to `body_strike +
    direction * offset_dollars`.

    `direction` is +1 (target sits above body — for call wings) or
    -1 (target sits below body — for put wings). Candidates are
    pre-filtered to the proper side of body before distance
    comparison so a closer strike on the wrong side never wins.

    Used as the fixed-dollar-width counterpart to delta-anchored
    wing selection. Addresses the C-1 finding: 5-delta wings on
    SPX put-skew produce $775-wide spreads with poor credit/risk;
    fixed-$25-width wings produce credit/risk consistent with
    practitioner-typical 20-30%.
    """
    if not candidates:
        return None
    if direction not in (-1, +1):
        raise ValueError(f"direction must be +1 or -1; got {direction}")
    if direction > 0:
        valid = [l for l in candidates if l.strike > body_strike]
    else:
        valid = [l for l in candidates if l.strike < body_strike]
    if not valid:
        return None
    target = body_strike + direction * offset_dollars
    return min(valid, key=lambda l: abs(l.strike - target))
