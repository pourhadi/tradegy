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
from tradegy.options.greeks import bs_greeks
from tradegy.options.positions import LegOrder, MultiLegOrder, MultiLegPosition
from tradegy.options.strategy import OptionStrategy


@dataclass(frozen=True)
class PutCreditSpread45dteD30(OptionStrategy):
    """Put credit spread with delta-anchored short + long legs.

    Default: 45 DTE, short put at -0.30 delta, long put at -0.05
    delta. The wide delta gap between short and long is what makes
    the position a credit spread (collect more on the short than
    we pay on the long).
    """

    target_dte: int = 45
    short_delta: float = 0.30
    wing_delta: float = 0.05
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
        if open_positions:
            return None

        expiry = self._pick_expiry(snapshot)
        if expiry is None:
            return None
        dte = (expiry - snapshot.ts_utc.date()).days
        if dte <= 0:
            return None
        T = dte / 365.0

        puts = sorted(
            [
                l for l in snapshot.for_expiry(expiry)
                if l.side == OptionSide.PUT and self._is_fillable(l)
            ],
            key=lambda l: l.strike,
        )
        if not puts:
            return None

        # Short put at -short_delta (the higher of the two strikes).
        short_put = self._closest_delta(
            puts, target=-self.short_delta,
            S=snapshot.underlying_price, T=T, r=snapshot.risk_free_rate,
        )
        # Long put at -wing_delta (the lower strike, OTM further).
        # Restrict candidates to strikes BELOW the short.
        below_short = [p for p in puts if p.strike < short_put.strike]
        long_put = self._closest_delta(
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

    # ── Internals (shared shape with IronCondor45dteD16; refactor
    # to a base class in a future round if a third strategy lands
    # the same shape) ──────────────────────────────────────────

    def _pick_expiry(self, snapshot: ChainSnapshot):
        snap_date = snapshot.ts_utc.date()
        expiries = snapshot.expiries()
        if not expiries:
            return None
        best = expiries[0]
        best_dist = abs((best - snap_date).days - self.target_dte)
        for e in expiries[1:]:
            d = (e - snap_date).days
            dist = abs(d - self.target_dte)
            if dist < best_dist or (dist == best_dist and e > best):
                best = e
                best_dist = dist
        return best

    @staticmethod
    def _is_fillable(leg: OptionLeg) -> bool:
        return leg.iv > 0.0 and (leg.bid > 0.0 or leg.ask > 0.0)

    @staticmethod
    def _closest_delta(
        candidates: list[OptionLeg], *,
        target: float, S: float, T: float, r: float,
    ) -> OptionLeg | None:
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
