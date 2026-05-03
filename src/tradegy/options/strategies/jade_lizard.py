"""Jade Lizard — defined-risk structure with no upside risk.

Practitioner-canon structure popularized by tastytrade. The
classic "naked" Jade Lizard is 3 legs (short put + short call
spread, no put wing) and has UNDEFINED downside risk. Per doc 14
risk-catalog item 1 (defined-risk only), we ship the DEFINED-
risk variant: 4 legs with a long put wing.

Structure:
  long  put (lower wing — defined-risk floor)
  short put (body — at higher delta than condor's body)
  short call (body)
  long  call (upper wing, NARROW — close to short call)

The defining property: the call spread is sized NARROW enough
that the total credit collected ≥ width of the call spread.
Result: at expiration, even if the underlying explodes upward
through both call legs, the call spread loss = call_spread_width
- credit_collected ≤ 0. Upside is risk-FREE.

Downside still has defined risk (long put wing limits it), but
the put body is closer to the money than a typical iron condor's
put body, so downside losses on the put spread can be larger per
contract than condor.

Mechanically: a Jade Lizard is an iron condor with asymmetric
delta tuning — short put body higher delta (more credit),
short call further OTM (less credit but less downside on a rip),
narrow call wing (caps upside cheaply). When the parameters
align so credit ≥ call_spread_width, the structure is "Jade
Lizard"; otherwise it's a regular asymmetric iron condor.

This class doesn't ENFORCE the credit ≥ width invariant at
selection time (the chain may not let us). It selects the
appropriate legs and reports the resulting credit / max loss;
the operator (or a downstream filter) decides whether to actually
enter.

Default parameters:

  target_dte = 45
  short_put_delta = 0.35     (closer to ATM than condor's 0.16)
  short_call_delta = 0.20    (further OTM than condor's 0.16)
  call_wing_delta = 0.10     (NARROW call wing — close to short call)
  put_wing_delta = 0.05      (standard put wing)
"""
from __future__ import annotations

from dataclasses import dataclass

from tradegy.options.chain import ChainSnapshot, OptionLeg, OptionSide
from tradegy.options.positions import LegOrder, MultiLegOrder, MultiLegPosition
from tradegy.options.strategies._helpers import (
    closest_delta,
    is_fillable,
    pick_expiry_closest_to_dte,
)
from tradegy.options.strategy import OptionStrategy


@dataclass(frozen=True)
class JadeLizard45dte(OptionStrategy):
    """Defined-risk Jade Lizard with delta-anchored leg selection.

    Parameters tuned so the typical SPX chain produces a structure
    where total credit ≥ call-spread width (zero upside risk).
    """

    target_dte: int = 45
    short_put_delta: float = 0.35
    short_call_delta: float = 0.20
    call_wing_delta: float = 0.10
    put_wing_delta: float = 0.05
    contracts: int = 1
    id: str = "jade_lizard_45dte"

    def on_chain(
        self,
        snapshot: ChainSnapshot,
        open_positions: tuple[MultiLegPosition, ...],
    ) -> MultiLegOrder | None:
        if open_positions:
            return None

        expiry = pick_expiry_closest_to_dte(snapshot, self.target_dte)
        if expiry is None:
            return None
        dte = (expiry - snapshot.ts_utc.date()).days
        if dte <= 0:
            return None
        T = dte / 365.0

        legs_at_e = snapshot.for_expiry(expiry)
        calls = sorted(
            [l for l in legs_at_e if l.side == OptionSide.CALL and is_fillable(l)],
            key=lambda l: l.strike,
        )
        puts = sorted(
            [l for l in legs_at_e if l.side == OptionSide.PUT and is_fillable(l)],
            key=lambda l: l.strike,
        )
        if not calls or not puts:
            return None

        S, r = snapshot.underlying_price, snapshot.risk_free_rate
        short_call = closest_delta(
            calls, target=self.short_call_delta, S=S, T=T, r=r,
        )
        short_put = closest_delta(
            puts, target=-self.short_put_delta, S=S, T=T, r=r,
        )
        # Narrow call wing: target call_wing_delta (e.g. 0.10),
        # restricted to strikes ABOVE short call.
        long_call = closest_delta(
            [c for c in calls if c.strike > short_call.strike],
            target=self.call_wing_delta, S=S, T=T, r=r,
        )
        # Standard put wing: target -put_wing_delta, restricted to
        # strikes BELOW short put.
        long_put = closest_delta(
            [p for p in puts if p.strike < short_put.strike],
            target=-self.put_wing_delta, S=S, T=T, r=r,
        )
        if (
            short_call is None or short_put is None
            or long_call is None or long_put is None
        ):
            return None
        # Defensive width invariants.
        if (
            long_call.strike <= short_call.strike
            or long_put.strike >= short_put.strike
        ):
            return None

        return MultiLegOrder(
            tag=self.id,
            contracts=self.contracts,
            legs=(
                LegOrder(
                    expiry=expiry, strike=long_put.strike,
                    side=OptionSide.PUT, quantity=+1,
                ),
                LegOrder(
                    expiry=expiry, strike=short_put.strike,
                    side=OptionSide.PUT, quantity=-1,
                ),
                LegOrder(
                    expiry=expiry, strike=short_call.strike,
                    side=OptionSide.CALL, quantity=-1,
                ),
                LegOrder(
                    expiry=expiry, strike=long_call.strike,
                    side=OptionSide.CALL, quantity=+1,
                ),
            ),
        )
