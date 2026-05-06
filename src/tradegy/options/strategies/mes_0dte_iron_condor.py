"""MES 0DTE iron condor — dollar-width strike selection.

Concrete 0DTE strategy class for MES futures options.  Differs from
the SPX/equity-options iron condors in two load-bearing ways:

  1. **No vendor IV.**  databento ohlcv-1m doesn't carry IV, so
     `closest_delta` (which calls `bs_greeks` with leg.iv) cannot
     pick legs.  Strikes are chosen by **dollar offset from
     underlying** instead.  This trades off practitioner-canon
     delta-targeting for a model that runs on the data we have.

  2. **bid==ask sentinel.**  The chain reader emits OptionLeg.bid =
     OptionLeg.ask = bar close.  The default `is_fillable` filter
     rejects locked quotes (ask <= bid), so we use a relaxed local
     filter that accepts bid == ask > 0 as a valid quote.

The structure: 4-leg defined-risk credit spread.

  Long put @ (S - put_short_offset - wing_width)
  Short put @ (S - put_short_offset)
  Short call @ (S + call_short_offset)
  Long call @ (S + call_short_offset + wing_width)

Defaults match a "wide" 0DTE iron condor for MES — short legs at
$50 from spot (~1% on a 5400 underlying), wings $25 wide for
defined risk.

Same-day expiry only (target_dte = 0).  If no expiry on the
snapshot date matches today, returns None — the snapshot is on a
weekend / holiday / pre-rollout day.

Concentration: at most one open position per strategy instance,
same as the existing iron condor classes.
"""
from __future__ import annotations

from dataclasses import dataclass

from tradegy.options.chain import ChainSnapshot, OptionLeg, OptionSide
from tradegy.options.positions import LegOrder, MultiLegOrder, MultiLegPosition
from tradegy.options.strategy import OptionStrategy


def _is_fillable_databento(leg: OptionLeg) -> bool:
    """Looser fillability check tolerant of the chain reader's
    bid==ask sentinel.

    The default is_fillable rejects locked quotes (ask <= bid).
    databento ohlcv-1m only gives us trades, so the chain reader
    emits bid = ask = bar close.  That's a valid trade, not a
    broken quote.  We accept it provided the close is positive.
    """
    return leg.bid > 0.0 and leg.ask >= leg.bid


def _closest_strike(legs: list[OptionLeg], target_strike: float) -> OptionLeg | None:
    """Return the leg whose strike is closest to `target_strike`."""
    if not legs:
        return None
    return min(legs, key=lambda l: abs(l.strike - target_strike))


@dataclass(frozen=True)
class Mes0dteIronCondor(OptionStrategy):
    """0DTE iron condor on MES with dollar-offset strike selection.

    Defaults: short legs at $50 from spot, wings $25 wide.  At an
    underlying around 5400, that's ~1% strike width on each side.
    """

    put_short_offset: float = 50.0
    call_short_offset: float = 50.0
    wing_width_dollars: float = 25.0
    contracts: int = 1
    id: str = "mes_0dte_iron_condor"

    def on_chain(
        self,
        snapshot: ChainSnapshot,
        open_positions: tuple[MultiLegPosition, ...],
    ) -> MultiLegOrder | None:
        if self._my_open(open_positions):
            return None

        S = snapshot.underlying_price
        if S <= 0.0:
            return None  # no underlying price → can't anchor strikes

        # Same-day expiry — target_dte = 0 means today.
        snap_date = snapshot.ts_utc.date()
        same_day_expiries = [e for e in snapshot.expiries() if e == snap_date]
        if not same_day_expiries:
            return None
        expiry = same_day_expiries[0]

        legs_at_e = snapshot.for_expiry(expiry)
        calls = sorted(
            [l for l in legs_at_e if l.side == OptionSide.CALL and _is_fillable_databento(l)],
            key=lambda l: l.strike,
        )
        puts = sorted(
            [l for l in legs_at_e if l.side == OptionSide.PUT and _is_fillable_databento(l)],
            key=lambda l: l.strike,
        )
        if not calls or not puts:
            return None

        # Short call: closest available strike to S + call_short_offset.
        # Filter to OTM calls only (strike > S) so we don't accidentally
        # pick an ITM strike when the chain is sparse near the money.
        otm_calls = [c for c in calls if c.strike > S]
        if not otm_calls:
            return None
        short_call = _closest_strike(otm_calls, S + self.call_short_offset)

        # Long call: closest strike at short_call.strike + wing_width.
        wider_calls = [c for c in otm_calls if c.strike > short_call.strike]
        if not wider_calls:
            return None
        long_call = _closest_strike(wider_calls, short_call.strike + self.wing_width_dollars)

        # Short put: closest available strike to S - put_short_offset.
        otm_puts = [p for p in puts if p.strike < S]
        if not otm_puts:
            return None
        short_put = _closest_strike(otm_puts, S - self.put_short_offset)

        # Long put: closest strike at short_put.strike - wing_width.
        wider_puts = [p for p in otm_puts if p.strike < short_put.strike]
        if not wider_puts:
            return None
        long_put = _closest_strike(wider_puts, short_put.strike - self.wing_width_dollars)

        # Defensive: wings must lie further from the body than the
        # short legs (positive wing width on both sides).
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
