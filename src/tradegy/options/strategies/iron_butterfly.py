"""Iron butterfly — concentrated iron condor with body strikes at ATM.

Same 4-leg shape as IronCondor45dteD16:
  long  put (lower wing)
  short put (body — at the ATM strike)
  short call (body — at the SAME ATM strike)
  long  call (upper wing)

Differs from iron condor in body placement: condor has body strikes
at lower-delta (e.g. 16-delta short legs sit OTM); butterfly puts
both short legs at the SAME strike, typically ATM. Result:

  - Maximum credit collected per trade (both shorts are nearer the
    money than they'd be on a condor with the same wing spacing).
  - Smallest profit zone — only profitable if underlying stays in
    a narrow band around the ATM strike.
  - Highest theta per dollar of risk — fast time decay near ATM.
  - Pin risk near expiration is severe (a small move past the ATM
    strike at expiry decimates the position). The 21-DTE management
    trigger is non-negotiable.

Practitioner usage: high-IV regimes where the operator believes the
underlying will trade narrowly. tastytrade research suggests iron
butterflies underperform iron condors on average due to the
narrower profit window, but they win bigger when the underlying
pins near the strike.

Default parameters:

  target_dte = 45
  wing_delta = 0.05    (5-delta long wings — same as condor)
  contracts = 1

There is no `short_delta` — the body is anchored to the ATM strike,
not a delta target.
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
class IronButterfly45dteAtm(OptionStrategy):
    """ATM iron butterfly: short call + short put both at the
    closest-to-spot strike, long wings at ±wing_delta.
    """

    target_dte: int = 45
    wing_delta: float = 0.05
    contracts: int = 1
    id: str = "iron_butterfly_45dte_atm"

    def on_chain(
        self,
        snapshot: ChainSnapshot,
        open_positions: tuple[MultiLegPosition, ...],
    ) -> MultiLegOrder | None:
        if self._my_open(open_positions):
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

        # ATM body strike: closest-to-spot strike that has BOTH a
        # fillable call and a fillable put.
        call_strikes = {c.strike: c for c in calls}
        put_strikes = {p.strike: p for p in puts}
        common_strikes = call_strikes.keys() & put_strikes.keys()
        if not common_strikes:
            return None
        atm_strike = min(
            common_strikes, key=lambda k: abs(k - snapshot.underlying_price),
        )
        short_call = call_strikes[atm_strike]
        short_put = put_strikes[atm_strike]

        # Long wings at delta target, OTM from body.
        long_call = closest_delta(
            [c for c in calls if c.strike > atm_strike],
            target=self.wing_delta,
            S=snapshot.underlying_price, T=T, r=snapshot.risk_free_rate,
        )
        long_put = closest_delta(
            [p for p in puts if p.strike < atm_strike],
            target=-self.wing_delta,
            S=snapshot.underlying_price, T=T, r=snapshot.risk_free_rate,
        )
        if long_call is None or long_put is None:
            return None
        # Defensive: wings strictly outside body.
        if (
            long_call.strike <= atm_strike
            or long_put.strike >= atm_strike
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
                    expiry=expiry, strike=atm_strike,
                    side=OptionSide.PUT, quantity=-1,
                ),
                LegOrder(
                    expiry=expiry, strike=atm_strike,
                    side=OptionSide.CALL, quantity=-1,
                ),
                LegOrder(
                    expiry=expiry, strike=long_call.strike,
                    side=OptionSide.CALL, quantity=+1,
                ),
            ),
        )
