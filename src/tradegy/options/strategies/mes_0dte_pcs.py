"""MES 0DTE put credit spread — dollar-width strike selection.

2-leg variant of `Mes0dteIronCondor`.  Sells a put credit spread:

    Long put @ (S - put_short_offset - wing_width)   [+1]
    Short put @ (S - put_short_offset)               [-1]

Same dollar-offset selection logic as the iron condor (no IV/delta
dependency).  Same-day-expiry filter (target_dte = 0).

Why this exists.  The iron condor v1 backtest (2023-2024) ran
NEGATIVE net P&L across every parameter variant tested — cost
($8 round-trip per IC) dominated the small per-trade premium
($3-5 gross median).  Halving the leg count to a 2-leg PCS
($4 RT cost) is the straightforward test of whether commissions
were the killer or if the underlying signal also has no edge.

Directional bias: bullish.  PCS profits when underlying stays
ABOVE the short put strike at expiry.  For 0DTE this is roughly
"underlying doesn't crash today".  Default short_offset = $50
(short put ~1% below spot) is similar premium to the IC's put
side but without the call-side hedge.

Same `_is_fillable_databento` and `_closest_strike` helpers from
the IC module.  Same concentration rule (one open position per
strategy instance).
"""
from __future__ import annotations

from dataclasses import dataclass

from tradegy.options.chain import ChainSnapshot, OptionLeg, OptionSide
from tradegy.options.positions import LegOrder, MultiLegOrder, MultiLegPosition
from tradegy.options.strategies.mes_0dte_iron_condor import (
    _closest_strike,
    _is_fillable_databento,
)
from tradegy.options.strategy import OptionStrategy


@dataclass(frozen=True)
class Mes0dtePcs(OptionStrategy):
    """0DTE put credit spread on MES with dollar-offset strike
    selection.

    Defaults: short put $50 below spot, $25 wing.
    """

    put_short_offset: float = 50.0
    wing_width_dollars: float = 25.0
    contracts: int = 1
    id: str = "mes_0dte_pcs"

    def on_chain(
        self,
        snapshot: ChainSnapshot,
        open_positions: tuple[MultiLegPosition, ...],
    ) -> MultiLegOrder | None:
        if self._my_open(open_positions):
            return None

        S = snapshot.underlying_price
        if S <= 0.0:
            return None

        snap_date = snapshot.ts_utc.date()
        same_day_expiries = [e for e in snapshot.expiries() if e == snap_date]
        if not same_day_expiries:
            return None
        expiry = same_day_expiries[0]

        legs_at_e = snapshot.for_expiry(expiry)
        puts = sorted(
            [l for l in legs_at_e if l.side == OptionSide.PUT and _is_fillable_databento(l)],
            key=lambda l: l.strike,
        )
        if not puts:
            return None

        otm_puts = [p for p in puts if p.strike < S]
        if not otm_puts:
            return None
        short_put = _closest_strike(otm_puts, S - self.put_short_offset)

        wider_puts = [p for p in otm_puts if p.strike < short_put.strike]
        if not wider_puts:
            return None
        long_put = _closest_strike(wider_puts, short_put.strike - self.wing_width_dollars)

        if long_put.strike >= short_put.strike:
            return None  # defensive

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
