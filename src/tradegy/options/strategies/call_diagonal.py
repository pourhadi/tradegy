"""Call diagonal spread — bearish-bias counterpart to PutDiagonal.

Structure (mirror of PutDiagonal):
  short  call: near expiry (e.g. 30 DTE), HIGHER delta (e.g. 30d)
  long   call: far  expiry (e.g. 60 DTE), LOWER  delta (e.g. 10d)
               — and HIGHER strike than the short

Different from CallCreditSpread (same expiration both legs):
  PCS/CCS: same expiration → loss capped at wing width minus credit.
  Diagonal: long leg has time value left at front expiration; can
            roll out / continue to capture decay differential.

Different from PutDiagonal: bearish bias instead of bullish. Profits
when underlying stays BELOW the short call strike at front
expiration.

Use case in the catalog: paired with PutDiagonal as a directional-
hedge bracket. PutDiagonal makes money in bull regimes; CallDiagonal
makes money in bear/range-bound regimes. Running both isn't
canonical (would offset directional exposure) but provides
diversification when either is gated by another regime feature.

Default parameters mirror PutDiagonal:
  target_dte_front = 30
  target_dte_back = 60
  short_delta = 0.30
  long_delta = 0.10

Long leg sits at HIGHER strike than short (bearish bias — long
strike represents the "ceiling" we expect underlying not to break).
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
class CallDiagonal30_60(OptionStrategy):
    """Bearish-bias call diagonal: short 30-DTE 30-delta call +
    long 60-DTE 10-delta call. Strike asymmetry produces bearish
    bias.
    """

    target_dte_front: int = 30
    target_dte_back: int = 60
    short_delta: float = 0.30
    long_delta: float = 0.10
    contracts: int = 1
    id: str = "call_diagonal_30_60_d30_d10"

    def on_chain(
        self,
        snapshot: ChainSnapshot,
        open_positions: tuple[MultiLegPosition, ...],
    ) -> MultiLegOrder | None:
        if open_positions:
            return None

        front = pick_expiry_closest_to_dte(snapshot, self.target_dte_front)
        back = pick_expiry_closest_to_dte(snapshot, self.target_dte_back)
        if front is None or back is None or back <= front:
            return None
        front_dte = (front - snapshot.ts_utc.date()).days
        back_dte = (back - snapshot.ts_utc.date()).days
        if front_dte <= 0 or back_dte <= 0:
            return None
        T_front = front_dte / 365.0
        T_back = back_dte / 365.0

        front_calls = sorted(
            [
                l for l in snapshot.for_expiry(front)
                if l.side == OptionSide.CALL and is_fillable(l)
            ],
            key=lambda l: l.strike,
        )
        back_calls = sorted(
            [
                l for l in snapshot.for_expiry(back)
                if l.side == OptionSide.CALL and is_fillable(l)
            ],
            key=lambda l: l.strike,
        )
        if not front_calls or not back_calls:
            return None

        # Short call on FRONT expiry at +short_delta (closer to spot).
        short_call = closest_delta(
            front_calls, target=self.short_delta,
            S=snapshot.underlying_price, T=T_front,
            r=snapshot.risk_free_rate,
        )
        # Long call on BACK expiry at +long_delta. Restrict to
        # strikes ABOVE the short (bearish bias — long protection
        # sits further OTM than short body).
        above_short = [c for c in back_calls if c.strike > short_call.strike]
        long_call = closest_delta(
            above_short, target=self.long_delta,
            S=snapshot.underlying_price, T=T_back,
            r=snapshot.risk_free_rate,
        )
        if short_call is None or long_call is None:
            return None
        if long_call.strike <= short_call.strike:
            return None

        return MultiLegOrder(
            tag=self.id,
            contracts=self.contracts,
            legs=(
                LegOrder(
                    expiry=front, strike=short_call.strike,
                    side=OptionSide.CALL, quantity=-1,
                ),
                LegOrder(
                    expiry=back, strike=long_call.strike,
                    side=OptionSide.CALL, quantity=+1,
                ),
            ),
        )
