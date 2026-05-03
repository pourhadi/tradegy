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


def is_fillable(leg: OptionLeg) -> bool:
    """Filter out dead-quote legs at strategy-side leg selection.

    A leg is fillable iff vendor IV is positive AND at least one
    side of the bid/ask has a positive quote. Strategy classes
    should never propose legs that won't fill at the runner.
    """
    return leg.iv > 0.0 and (leg.bid > 0.0 or leg.ask > 0.0)


def closest_delta(
    candidates: list[OptionLeg], *,
    target: float,
    S: float, T: float, r: float,
) -> OptionLeg | None:
    """Return the leg whose Black-Scholes delta is closest to
    `target`. Sign convention: call delta > 0, put delta < 0;
    pass signed target (+0.16 for 16-delta call, -0.16 for
    16-delta put).

    Empty candidates → None.
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
    return best
