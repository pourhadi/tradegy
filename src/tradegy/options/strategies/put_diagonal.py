"""Put diagonal spread — different from calendar (different STRIKES
on the two expiries).

Calendar: short near-month put + long far-month put at SAME strike.
Diagonal: short near-month put + long far-month put at DIFFERENT
strikes.

Default structure (defensive bullish put diagonal):
  short  put: near expiry (e.g. 30 DTE), HIGHER delta (e.g. 30d)
              — collects fast-decaying premium near the money
  long   put: far  expiry (e.g. 60 DTE), LOWER  delta (e.g. 10d)
              — defensive far-OTM protection that decays slowly

Net: usually a small CREDIT (the near-30d put pricier than the
far-10d put despite the longer expiration). Differentiated from
calendar:
  - Calendar (same strike) profits when underlying lands ON the
    strike at front expiration. Pure non-directional decay capture.
  - Diagonal (long < short strike) profits when underlying STAYS
    ABOVE the short strike at front expiration. Directional
    bullish bias.

Compared to a put credit spread (same expiration both legs):
  - PCS pays 0% theta on the long leg (same-expiration decay
    matches short).
  - Diagonal: long leg's theta is much smaller per day than
    short leg's → net positive theta accrues to position holder.

Risk profile:
  - Front-leg expiration: short_K_short - max(K_short - U, 0).
    If U > K_short: short put OTM, full short credit captured.
    If K_long < U < K_short: short put hit, long put OTM. Loss = K_short - U.
    If U < K_long: short hit + long protective. Loss capped at
                   K_short - K_long - net_credit.
  - Long leg has time value left at front expiration; recovery
    possible via roll-out.

Default parameters:
  target_dte_front = 30
  target_dte_back = 60
  short_delta = 0.30
  long_delta = 0.10
  contracts = 1

Per `14_options_volatility_selling.md` — debit-position-aware
management is OK because diagonals are typically slight credits
but can flip to debit in some chain conditions; the strategy
classes don't enforce sign, the runner adapts.
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
class PutDiagonal30_60(OptionStrategy):
    """Bullish-bias put diagonal: short 30-DTE 30-delta + long
    60-DTE 10-delta. Strike asymmetry produces directional bias
    (vs the calendar's pure non-directional structure).

    Concentration rule: at most one open position per strategy.
    """

    target_dte_front: int = 30
    target_dte_back: int = 60
    short_delta: float = 0.30
    long_delta: float = 0.10
    contracts: int = 1
    id: str = "put_diagonal_30_60_d30_d10"

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

        front_puts = sorted(
            [
                l for l in snapshot.for_expiry(front)
                if l.side == OptionSide.PUT and is_fillable(l)
            ],
            key=lambda l: l.strike,
        )
        back_puts = sorted(
            [
                l for l in snapshot.for_expiry(back)
                if l.side == OptionSide.PUT and is_fillable(l)
            ],
            key=lambda l: l.strike,
        )
        if not front_puts or not back_puts:
            return None

        # Short put on FRONT expiry at -short_delta.
        short_put = closest_delta(
            front_puts, target=-self.short_delta,
            S=snapshot.underlying_price, T=T_front,
            r=snapshot.risk_free_rate,
        )
        # Long put on BACK expiry at -long_delta. Restrict to strikes
        # BELOW the short strike (so the diagonal is bullish-biased
        # — long protection sits further OTM than short body).
        below_short = [p for p in back_puts if p.strike < short_put.strike]
        long_put = closest_delta(
            below_short, target=-self.long_delta,
            S=snapshot.underlying_price, T=T_back,
            r=snapshot.risk_free_rate,
        )
        if short_put is None or long_put is None:
            return None
        if long_put.strike >= short_put.strike:
            return None  # defensive — must be diagonal not vertical

        return MultiLegOrder(
            tag=self.id,
            contracts=self.contracts,
            legs=(
                LegOrder(
                    expiry=front, strike=short_put.strike,
                    side=OptionSide.PUT, quantity=-1,
                ),
                LegOrder(
                    expiry=back, strike=long_put.strike,
                    side=OptionSide.PUT, quantity=+1,
                ),
            ),
        )
