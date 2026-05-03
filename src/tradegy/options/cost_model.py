"""Option-fill cost model — per-leg commission + mid-price slippage.

Different from the bar-driven harness's tick-slippage model. Option
fills don't have a tick-size analogue at retail; the slippage you
actually pay is some fraction of the bid-ask spread, plus per-leg
commission.

Defaults reflect IBKR's common 2026 retail tier for SPX options:

    - $0.65/contract per leg (no exchange/clearing pass-through
      itemized; included in the per-contract figure)
    - Multi-leg combo orders fill at mid ± `spread_offset_fraction`
      of the bid-ask half-spread, per leg

The fill convention models how mid-price limit orders typically
get filled by a market maker: not exactly at mid, but within the
spread, biased slightly toward the side that benefits the MM.
For credit positions (we sell): expected fill = mid - half_spread *
spread_offset_fraction (slightly below mid, MM gets the better side).
For debit positions (we buy): expected fill = mid + half_spread *
spread_offset_fraction (slightly above mid).

Per `14_options_volatility_selling.md` Phase B requirements: the
harness must be able to model maker / taker semantics so backtest
P&L tracks paper P&L within ±15%. Mid-price ± fraction is a
crude-but-honest first cut; later phases can extend to per-leg
volume-weighted or DOM-aware fills if the paper-vs-backtest gap
demands it.
"""
from __future__ import annotations

from dataclasses import dataclass

from tradegy.options.chain import OptionLeg


@dataclass(frozen=True)
class OptionCostModel:
    """Per-leg cost + fill assumptions for option backtests.

    `commission_per_leg` is per-contract per-leg. A 1-lot iron
    condor (4 legs) at $0.65/leg pays $2.60 to open + $2.60 to
    close = $5.20 round-trip per lot.

    `spread_offset_fraction` is in [0, 1]. 0 = fill at exact mid
    (optimistic). 1 = fill at the far side of the bid-ask (worst
    case). Default 0.20 reflects typical retail fills on liquid
    SPX strikes; widen to 0.50+ for stress-regime simulation.
    """

    commission_per_leg: float = 0.65
    spread_offset_fraction: float = 0.20

    def fill_price(self, leg: OptionLeg, *, signed_quantity: int) -> float:
        """Expected per-share fill price for a leg ordered at signed
        `quantity` against the leg's current quote. Returns the
        per-share price (positive); the caller multiplies by sign-
        of-quantity for cash-flow accounting.

        Long (qty > 0): pay slightly above mid → mid + offset
        Short (qty < 0): receive slightly below mid → mid - offset

        If bid + ask are both zero (locked / dropped quote) we fall
        back to leg.iv-implied intrinsic floor — but that case
        should be filtered out at strategy-class submission time
        (don't try to fill on a dead quote).
        """
        if leg.bid <= 0.0 and leg.ask <= 0.0:
            return 0.0
        if leg.bid <= 0.0 or leg.ask <= 0.0:
            return leg.mid
        half_spread = 0.5 * (leg.ask - leg.bid)
        offset = half_spread * self.spread_offset_fraction
        if signed_quantity > 0:
            return leg.mid + offset
        return leg.mid - offset

    def commission_for_legs(self, n_legs: int, *, contracts: int = 1) -> float:
        """Total commission in dollars to open OR close `n_legs`
        legs at `contracts` lots. Symmetric — closing pays again.
        """
        return self.commission_per_leg * n_legs * contracts

    def round_trip_commission(self, n_legs: int, *, contracts: int = 1) -> float:
        """Convenience: open + close commission for one position."""
        return 2.0 * self.commission_for_legs(n_legs, contracts=contracts)
