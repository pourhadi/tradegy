"""Put broken-wing butterfly (PBWB) — asymmetric, defined-risk,
typically credit-yielding.

Practitioner-canon credit-style PBWB structure (4 puts, all puts):

  Strike order (highest → lowest): K1 > K2 > K3
  Quantity:                        +1 long  -2 short  +1 long
  Width labels:                    inner    body     outer
                                   (K1-K2)            (K2-K3)

  inner = K1 - K2  (NARROWER wing — the long leg closer to spot)
  outer = K2 - K3  (WIDER wing — the long leg further OTM)

  Net premium = (2 × short_K2_credit) - long_K1_debit - long_K3_debit
              = typically POSITIVE (credit) when inner is much
                narrower than outer

Payoff at expiration (underlying = U):
  U > K1:           credit only — UNCAPPED upside but bounded at 0 from
                    a payoff perspective (we just keep the credit).
  K2 < U < K1:      credit + (K1 - U)         peaks at U = K2 (max profit)
  U = K2:           credit + (K1 - K2) = credit + inner_width
  K3 < U < K2:      credit + (K1 - K2) + (K2 - U) - 2*(K2 - U)
                    = credit + inner_width - (K2 - U)
                    declines linearly as U falls below K2
  U < K3:           credit + inner_width - outer_width

Max profit: credit + inner_width (at expiration with U landing on K2).
Max loss:   abs(credit + inner_width - outer_width)
            = outer_width - inner_width - credit  (typically positive)

The "no risk on one side" property: when net credit ≥ 0, the payoff
ABOVE K1 (the upside) is just the credit collected — UNCAPPED upside
profit, NO upside loss. The downside (below K3) caps at the
asymmetric width difference minus credit.

Default parameters:
  target_dte = 45
  body_delta = 0.20            (the K2 short body — somewhat aggressive)
  inner_wing_dollars = 25.0    (K1 - K2)
  outer_wing_dollars = 75.0    (K2 - K3 — 3x the inner wing)
  contracts = 1

For SPX, $25 inner / $75 outer is a typical practitioner setup that
yields a small credit at 20-delta short puts. The structure is much
more capital-efficient per dollar of risk than a pure iron condor
because the upside is risk-free.
"""
from __future__ import annotations

from dataclasses import dataclass

from tradegy.options.chain import ChainSnapshot, OptionLeg, OptionSide
from tradegy.options.positions import LegOrder, MultiLegOrder, MultiLegPosition
from tradegy.options.strategies._helpers import (
    closest_delta,
    closest_strike_at_offset,
    is_fillable,
    pick_expiry_closest_to_dte,
)
from tradegy.options.strategy import OptionStrategy


@dataclass(frozen=True)
class PutBrokenWingButterfly45dte(OptionStrategy):
    """4-leg put BWB. Long inner wing + 2 short body + long outer wing.

    Body strike picked by delta target. Wing strikes picked by fixed
    dollar offset (not delta) so the structure is a true butterfly
    with controllable wing widths.

    Concentration rule: at most one open position per strategy
    instance.
    """

    target_dte: int = 45
    body_delta: float = 0.20
    inner_wing_dollars: float = 25.0
    outer_wing_dollars: float = 75.0
    contracts: int = 1
    id: str = "put_broken_wing_butterfly_45dte_d20"

    def on_chain(
        self,
        snapshot: ChainSnapshot,
        open_positions: tuple[MultiLegPosition, ...],
    ) -> MultiLegOrder | None:
        if self._my_open(open_positions):
            return None
        if self.outer_wing_dollars <= self.inner_wing_dollars:
            # The structural definition of "broken-wing" requires
            # asymmetric wings. Equal wings → standard butterfly.
            return None

        expiry = pick_expiry_closest_to_dte(snapshot, self.target_dte)
        if expiry is None:
            return None
        dte = (expiry - snapshot.ts_utc.date()).days
        if dte <= 0:
            return None
        T = dte / 365.0

        puts = sorted(
            [
                l for l in snapshot.for_expiry(expiry)
                if l.side == OptionSide.PUT and is_fillable(l)
            ],
            key=lambda l: l.strike,
        )
        if not puts:
            return None

        # Body: closest-to-target-delta put. Sign convention: PUT
        # delta is negative; pass -body_delta for "20-delta put."
        body = closest_delta(
            puts, target=-self.body_delta,
            S=snapshot.underlying_price, T=T,
            r=snapshot.risk_free_rate,
        )
        if body is None:
            return None

        # Inner wing (long, K1 = body + inner_wing): closer to spot
        # than body. For puts, "closer to spot" = HIGHER strike.
        long_inner = closest_strike_at_offset(
            puts,
            body_strike=body.strike,
            offset_dollars=self.inner_wing_dollars,
            direction=+1,  # higher strike than body
        )
        # Outer wing (long, K3 = body - outer_wing): further OTM,
        # below body.
        long_outer = closest_strike_at_offset(
            puts,
            body_strike=body.strike,
            offset_dollars=self.outer_wing_dollars,
            direction=-1,  # lower strike than body
        )
        if long_inner is None or long_outer is None:
            return None
        # Defensive: enforce K1 > K2 > K3 with positive separations.
        if (
            long_inner.strike <= body.strike
            or long_outer.strike >= body.strike
        ):
            return None

        return MultiLegOrder(
            tag=self.id,
            contracts=self.contracts,
            legs=(
                # Order: lowest strike to highest. Two short body
                # quantities are the same strike (the broker treats
                # them as a single -2 ratio at that strike).
                LegOrder(
                    expiry=expiry, strike=long_outer.strike,
                    side=OptionSide.PUT, quantity=+1,
                ),
                LegOrder(
                    expiry=expiry, strike=body.strike,
                    side=OptionSide.PUT, quantity=-2,
                ),
                LegOrder(
                    expiry=expiry, strike=long_inner.strike,
                    side=OptionSide.PUT, quantity=+1,
                ),
            ),
        )
