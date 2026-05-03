"""ShortStrangleDefined — defined-risk short strangle (4 legs).

Same shape as the iron condor (short call + long call wing + short
put + long put wing), but with a NARROWER body — the short legs sit
closer to ATM. The trade-off vs IronCondor45dteD16:

  Iron condor (short body 16-delta)
    - body further OTM → ~70% probability of expiring fully OTM
    - smaller per-trade premium
    - slower decay (more time-value to bleed off)
    - more "room" for the underlying to move within the body
      before testing the short legs

  Defined-risk strangle (short body 25-30 delta)
    - body closer to ATM → ~50-60% probability of expiring fully OTM
    - larger per-trade premium (~2-3x the condor's credit)
    - faster decay (more theta per day per dollar of risk)
    - SHORT legs are tested faster on adverse moves; pin risk
      near expiration is much higher (this is what the 21 DTE
      management trigger is non-negotiable for)

Practitioner usage: defined-risk strangles are the higher-octane
sibling of the iron condor. Same management discipline applies;
the position behavior and capital efficiency profile is different.

Default parameters per practitioner-canon:

  target_dte = 45
  short_delta = 0.25   (closer to ATM than the condor's 0.16)
  wing_delta = 0.05    (same outer wing delta as condor — the
                        body is what's narrower, not the wings)

Implementation: identical leg-selection mechanics as
IronCondor45dteD16; only the short_delta default differs. Sharing
the helpers in `_helpers.py` keeps the two classes from drifting
apart on shared concerns (delta-target search, expiry pick,
fillable filter).
"""
from __future__ import annotations

from dataclasses import dataclass

from tradegy.options.chain import ChainSnapshot, OptionSide
from tradegy.options.positions import LegOrder, MultiLegOrder, MultiLegPosition
from tradegy.options.strategies._helpers import (
    closest_delta,
    closest_strike_at_offset,
    is_fillable,
    pick_expiry_closest_to_dte,
)
from tradegy.options.strategy import OptionStrategy


@dataclass(frozen=True)
class ShortStrangleDefined45dteD25(OptionStrategy):
    """Defined-risk short strangle: 25-delta short body, 5-delta
    long wings, 45 DTE. Concentration rule: at most one open
    position per strategy instance (Phase B-3 RiskManager handles
    portfolio-level capital cap).

    Optional `wing_width_dollars` overrides delta-anchored wings
    with width-anchored — see PutCreditSpread for the rationale.
    """

    target_dte: int = 45
    short_delta: float = 0.25
    wing_delta: float = 0.05
    wing_width_dollars: float | None = None
    contracts: int = 1
    id: str = "short_strangle_defined_45dte_d25"

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

        short_call = closest_delta(
            calls, target=self.short_delta,
            S=snapshot.underlying_price, T=T, r=snapshot.risk_free_rate,
        )
        short_put = closest_delta(
            puts, target=-self.short_delta,
            S=snapshot.underlying_price, T=T, r=snapshot.risk_free_rate,
        )
        if self.wing_width_dollars is not None:
            long_call = closest_strike_at_offset(
                calls, body_strike=short_call.strike,
                offset_dollars=self.wing_width_dollars, direction=+1,
            )
            long_put = closest_strike_at_offset(
                puts, body_strike=short_put.strike,
                offset_dollars=self.wing_width_dollars, direction=-1,
            )
        else:
            long_call = closest_delta(
                [c for c in calls if c.strike > short_call.strike],
                target=self.wing_delta,
                S=snapshot.underlying_price, T=T, r=snapshot.risk_free_rate,
            )
            long_put = closest_delta(
                [p for p in puts if p.strike < short_put.strike],
                target=-self.wing_delta,
                S=snapshot.underlying_price, T=T, r=snapshot.risk_free_rate,
            )

        if (
            short_call is None or short_put is None
            or long_call is None or long_put is None
        ):
            return None
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
