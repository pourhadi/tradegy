"""Put credit spread (bull put spread) — directional defined-risk.

Sells a higher-strike put and buys a lower-strike put for
protection. Net premium received; max loss = (short_strike -
long_strike) * multiplier - credit. Profits when underlying stays
above the short strike at expiry.

Different from the iron condor in three load-bearing ways:

  - Two legs, not four. Smaller buying-power footprint per
    contract.
  - Directional: positive delta exposure (we benefit if
    underlying rises). Iron condors are roughly delta-neutral at
    entry; the credit spread is a directional bet that the
    underlying won't drop below the short strike.
  - Single-side, so the position has all its premium concentrated
    on one side of the chain. Higher per-trade vega exposure
    than a comparably-sized iron condor (no offsetting call wing).

Same shared infrastructure: management triggers (50% / 21 DTE /
200% loss) come from the runner; risk gates (capital cap,
per-expiration cap) come from the RiskManager.

Default parameters per practitioner-canon (tastytrade research):

  target_dte = 45
  short_delta = 0.30   (30-delta short put: meaningful premium,
                         ~70% probability of expiring OTM)
  wing_delta = 0.05    (5-delta long wing: minimal cost protection
                         that defines the risk)

Subclassable for variants (e.g. PutCreditSpread30dteD20 with
target_dte=30, short_delta=0.20).
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
class PutCreditSpread45dteD30(OptionStrategy):
    """Put credit spread with delta-anchored short leg + dual-mode
    wing selection.

    Default: 45 DTE, short put at -0.30 delta, long wing at -0.05
    delta.

    Optional `wing_width_dollars`: when set (non-None), overrides
    `wing_delta` and selects the long-wing strike as the closest
    available to (short_strike - wing_width_dollars). Addresses
    the 2026-05-03 real-data finding that 5-delta wings on SPX
    put-skew produce $775-wide spreads with only 9% credit/risk
    vs the practitioner-typical 20-30%.
    """

    target_dte: int = 45
    short_delta: float = 0.30
    wing_delta: float = 0.05
    wing_width_dollars: float | None = None
    contracts: int = 1
    id: str = "put_credit_spread_45dte_d30"

    def on_chain(
        self,
        snapshot: ChainSnapshot,
        open_positions: tuple[MultiLegPosition, ...],
    ) -> MultiLegOrder | None:
        # Same concentration rule as the iron condor: at most one
        # open position per strategy instance. Phase B-3's
        # RiskManager handles the global capital cap.
        if self._my_open(open_positions):
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

        # Short put at -short_delta (the higher of the two strikes).
        short_put = closest_delta(
            puts, target=-self.short_delta,
            S=snapshot.underlying_price, T=T, r=snapshot.risk_free_rate,
        )
        # Long-wing selection: width-anchored if wing_width_dollars
        # is set; otherwise delta-anchored (default).
        if self.wing_width_dollars is not None:
            long_put = closest_strike_at_offset(
                puts,
                body_strike=short_put.strike,
                offset_dollars=self.wing_width_dollars,
                direction=-1,
            )
        else:
            below_short = [p for p in puts if p.strike < short_put.strike]
            long_put = closest_delta(
                below_short, target=-self.wing_delta,
                S=snapshot.underlying_price, T=T, r=snapshot.risk_free_rate,
            )

        if short_put is None or long_put is None:
            return None
        # Defensive: long must sit BELOW short (positive spread width).
        if long_put.strike >= short_put.strike:
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
            ),
        )

