"""Reverse Iron Condor (RIC) — long-vol structure that profits on
big moves either direction.

Mirror of the standard iron condor:
  Long put (lower strike, body)
  Short put (lower strike, wing — further OTM)
  Short call (upper strike, wing — further OTM)
  Long call (upper strike, body)

NET DEBIT — we PAY premium up front to acquire the position. Profit
when underlying moves OUT of the body (above upper-call body or
below lower-put body) at expiration. Max profit = body width minus
debit (per side). Max loss = debit paid (when underlying lands
between the two body strikes — neither side ITM).

Different from a long strangle (no wings) in that the wings cap
the upside profit — defined-risk on profit AND on loss.

Use case for the platform: PORTFOLIO HEDGE for the short-vol
majority. When an unexpected vol spike hits (2020-03 COVID, 2022
Q1, etc.), the RIC profits while the iron condors / credit spreads
get hit. Tiny allocation (5-10% of capital) acts as a tail-risk
floor.

Default parameters (ATM-near body):
  target_dte = 45
  body_delta = 0.30        (closer to ATM than IC's 0.16 — body
                            captures more directional move)
  wing_delta = 0.10        (still OTM)

Management semantics: this is a DEBIT position so:
  - profit_take_pct_of_debit (default 0.50 = "close at 50% gain")
  - loss_stop_pct_of_debit (default 0.75 = "close at 75% loss")
  - dte_close (default 21 — same as credit positions; debit
    positions also need to manage gamma risk near expiry)

Inverted vs IronCondor: short legs are FURTHER OTM than long legs,
so the wings collect a small credit while the long body legs cost
the most. Net debit = (long_call + long_put) - (short_call + short_put).
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
class ReverseIronCondor45dteD30(OptionStrategy):
    """4-leg debit reverse iron condor: long body at 30-delta,
    short wings at 10-delta. Profits on big directional moves.

    Concentration rule: at most one open position per strategy
    instance.
    """

    target_dte: int = 45
    body_delta: float = 0.30
    wing_delta: float = 0.10
    contracts: int = 1
    id: str = "reverse_iron_condor_45dte_d30"

    def on_chain(
        self,
        snapshot: ChainSnapshot,
        open_positions: tuple[MultiLegPosition, ...],
    ) -> MultiLegOrder | None:
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

        # LONG body legs at +/- body_delta — we BUY these at higher
        # premium.
        long_call = closest_delta(
            calls, target=self.body_delta,
            S=snapshot.underlying_price, T=T, r=snapshot.risk_free_rate,
        )
        long_put = closest_delta(
            puts, target=-self.body_delta,
            S=snapshot.underlying_price, T=T, r=snapshot.risk_free_rate,
        )
        # SHORT wing legs at +/- wing_delta — we SELL these at
        # lower premium for partial offset. Restrict to strikes
        # OUTSIDE the body so we have positive wing-vs-body separation.
        short_call = closest_delta(
            [c for c in calls if c.strike > long_call.strike],
            target=self.wing_delta,
            S=snapshot.underlying_price, T=T, r=snapshot.risk_free_rate,
        )
        short_put = closest_delta(
            [p for p in puts if p.strike < long_put.strike],
            target=-self.wing_delta,
            S=snapshot.underlying_price, T=T, r=snapshot.risk_free_rate,
        )
        if (
            long_call is None or long_put is None
            or short_call is None or short_put is None
        ):
            return None
        if (
            short_call.strike <= long_call.strike
            or short_put.strike >= long_put.strike
        ):
            return None  # defensive — wings must be outside body

        return MultiLegOrder(
            tag=self.id,
            contracts=self.contracts,
            legs=(
                # Lowest to highest strike, with the inversion of
                # the standard iron condor's signs.
                LegOrder(
                    expiry=expiry, strike=short_put.strike,
                    side=OptionSide.PUT, quantity=-1,
                ),
                LegOrder(
                    expiry=expiry, strike=long_put.strike,
                    side=OptionSide.PUT, quantity=+1,
                ),
                LegOrder(
                    expiry=expiry, strike=long_call.strike,
                    side=OptionSide.CALL, quantity=+1,
                ),
                LegOrder(
                    expiry=expiry, strike=short_call.strike,
                    side=OptionSide.CALL, quantity=-1,
                ),
            ),
        )
