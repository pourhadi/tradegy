"""Iron condor with delta-anchored entry + delta-anchored wings.

Concrete vol-selling strategy class. Per
`14_options_volatility_selling.md` Phase B-2:

  Entry rules
    - Skip if any position is already open (concentration limit
      enforced by the strategy in MVP; later moves to a portfolio-
      level cap in Phase B-3).
    - Pick expiry whose DTE is closest to `target_dte` (default 45).
      Tie-break: prefer the later expiry (more management headroom).
    - Short call: closest to +`short_delta` (default +0.16) call
      delta.
    - Short put: closest to -`short_delta` put delta.
    - Long wings: closest to ±`wing_delta` (default ±0.05). Delta-
      anchored wings are the FIX for the asymmetric-wings issue we
      surfaced in B-1's real-data smoke test (next-strike wings
      produced a $25 put wing + $100 call wing because SPX strike
      spacing varies by distance from spot).

  Management
    Inherited from ManagementRules + should_close (50% profit / 21
    DTE / 200% loss). The strategy class never decides exits.

The strategy is stateless — every instance configured with the same
parameters produces the same output for the same (snapshot,
open_positions) input. State (open positions, P&L, fills) lives in
the runner.
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
class IronCondor45dteD16(OptionStrategy):
    """Default iron condor: 45 DTE, 16-delta short body, 5-delta
    long wings, 1 contract per entry.

    Subclassable for variants — e.g. IronCondor45dteD10 with
    short_delta=0.10, IronCondor30dteD16 with target_dte=30.
    Optional `wing_width_dollars` overrides delta-anchored wings
    with width-anchored — see PutCreditSpread for the rationale
    (5-delta wings on SPX put-skew yield poor credit/risk).
    """

    target_dte: int = 45
    short_delta: float = 0.16
    wing_delta: float = 0.05
    wing_width_dollars: float | None = None
    contracts: int = 1
    id: str = "iron_condor_45dte_d16"

    def on_chain(
        self,
        snapshot: ChainSnapshot,
        open_positions: tuple[MultiLegPosition, ...],
    ) -> MultiLegOrder | None:
        # Phase-B-2 concentration rule: at most one position open
        # at a time. Phase B-3 promotes this to a portfolio-level
        # capital-percentage cap.
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

        # Delta-anchored leg selection.
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

        # Defensive sanity: wings must be FURTHER from the body
        # than the short legs (positive wing width on each side).
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

