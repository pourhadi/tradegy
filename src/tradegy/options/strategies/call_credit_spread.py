"""Call credit spread (bear call spread) — directional defined-risk.

Mirror of PutCreditSpread45dteD30. Sells a higher-delta call near
the money, buys a further-OTM call wing for protection. Net premium
received; max loss = (long_strike - short_strike) * multiplier -
credit. Profits when underlying stays BELOW the short strike at
expiry.

Bearish bias — useful in down-trending or range-bound regimes where
the put-side directional bet would lose. The 2025 SPX bull trend
favored put credit spreads; a bear or sideways year would favor
call credit spreads. Running both sides (e.g. iron condor) hedges
the directional bet at the cost of less premium per side.

Default parameters per practitioner-canon (mirrors the PCS):

  target_dte = 45
  short_delta = 0.30   (30-delta short call)
  wing_delta = 0.05    (5-delta long wing)

Same management / risk / runner integration as every other
strategy class.
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
class CallCreditSpread45dteD30(OptionStrategy):
    """Call credit spread with delta-anchored short leg + dual-mode
    wing selection.

    Default: 45 DTE, short call at +0.30 delta, long wing at +0.05
    delta. Optional `wing_width_dollars` overrides delta-anchored
    wing for width-anchored — see PutCreditSpread for the rationale.
    """

    target_dte: int = 45
    short_delta: float = 0.30
    wing_delta: float = 0.05
    wing_width_dollars: float | None = None
    contracts: int = 1
    id: str = "call_credit_spread_45dte_d30"

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

        calls = sorted(
            [
                l for l in snapshot.for_expiry(expiry)
                if l.side == OptionSide.CALL and is_fillable(l)
            ],
            key=lambda l: l.strike,
        )
        if not calls:
            return None

        # Short call at +short_delta (the lower of the two strikes —
        # closer to spot for a 30-delta call; further OTM for a
        # smaller delta).
        short_call = closest_delta(
            calls, target=self.short_delta,
            S=snapshot.underlying_price, T=T, r=snapshot.risk_free_rate,
        )
        # Long-wing selection: width-anchored if wing_width_dollars
        # is set; otherwise delta-anchored.
        if self.wing_width_dollars is not None:
            long_call = closest_strike_at_offset(
                calls,
                body_strike=short_call.strike,
                offset_dollars=self.wing_width_dollars,
                direction=+1,
            )
        else:
            above_short = [c for c in calls if c.strike > short_call.strike]
            long_call = closest_delta(
                above_short, target=self.wing_delta,
                S=snapshot.underlying_price, T=T, r=snapshot.risk_free_rate,
            )

        if short_call is None or long_call is None:
            return None
        # Defensive: long must sit ABOVE short (positive spread width).
        if long_call.strike <= short_call.strike:
            return None

        return MultiLegOrder(
            tag=self.id,
            contracts=self.contracts,
            legs=(
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
