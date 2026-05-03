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
from tradegy.options.greeks import bs_greeks
from tradegy.options.positions import LegOrder, MultiLegOrder, MultiLegPosition
from tradegy.options.strategy import OptionStrategy


@dataclass(frozen=True)
class IronCondor45dteD16(OptionStrategy):
    """Default iron condor: 45 DTE, 16-delta short body, 5-delta
    long wings, 1 contract per entry.

    Subclassable for variants — e.g. IronCondor45dteD10 with
    short_delta=0.10, IronCondor30dteD16 with target_dte=30, etc.
    """

    target_dte: int = 45
    short_delta: float = 0.16
    wing_delta: float = 0.05
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

        expiry = self._pick_expiry(snapshot)
        if expiry is None:
            return None
        dte = (expiry - snapshot.ts_utc.date()).days
        if dte <= 0:
            return None
        T = dte / 365.0

        legs_at_e = snapshot.for_expiry(expiry)
        calls = sorted(
            [l for l in legs_at_e if l.side == OptionSide.CALL and self._is_fillable(l)],
            key=lambda l: l.strike,
        )
        puts = sorted(
            [l for l in legs_at_e if l.side == OptionSide.PUT and self._is_fillable(l)],
            key=lambda l: l.strike,
        )
        if not calls or not puts:
            return None

        # Delta-anchored leg selection.
        short_call = self._closest_delta(
            calls, target=self.short_delta,
            S=snapshot.underlying_price, T=T, r=snapshot.risk_free_rate,
        )
        short_put = self._closest_delta(
            puts, target=-self.short_delta,
            S=snapshot.underlying_price, T=T, r=snapshot.risk_free_rate,
        )
        long_call = self._closest_delta(
            [c for c in calls if c.strike > short_call.strike],
            target=self.wing_delta,
            S=snapshot.underlying_price, T=T, r=snapshot.risk_free_rate,
        )
        long_put = self._closest_delta(
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

    # ── Internals ───────────────────────────────────────────────

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
        """Return the leg whose Black-Scholes delta is closest to
        `target`. Sign convention: call delta > 0, put delta < 0.
        Use signed `target` (e.g. +0.16 for 16-delta call, -0.16 for
        16-delta put).

        Empty candidates → None. Vendor IV could in principle be
        zero for an illiquid strike; we filter those at the
        caller via _is_fillable so this is safe to ignore here.
        """
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
