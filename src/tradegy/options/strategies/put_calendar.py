"""PutCalendarSpread — debit-position term-structure capture.

Two legs at SAME strike, DIFFERENT expiries:

  - Short the front-month put (collect fast-decaying premium)
  - Long the back-month put at the same strike (slower decay,
    provides protection)

Net DEBIT: the back-month is more expensive than the front, so
we pay net premium at entry. Max loss = the debit paid (when
both puts expire worthless OR when the spread compresses to
zero at front expiration).

Edge thesis: front-month time value decays faster than back-
month (theta is highest in the final ~30 DTE). Capturing the
differential decay produces profit when the underlying stays
near the strike at front expiration. Profit decays as the
underlying moves away from the strike.

Different from credit spreads in three load-bearing ways:

  - DEBIT, not credit. Capital reservation = debit paid (much
    smaller than the credit-spread max-loss numbers we've seen).
  - Two expiries, not one. The 21-DTE management trigger fires
    on the FRONT expiry (already correct in
    `MultiLegPosition.days_to_expiry` which returns the nearest).
  - Profit/loss is measured against the DEBIT, not the credit.
    Strategy classes that hold calendars MUST pass an explicit
    `ManagementRules` with `profit_take_pct_of_debit` and
    `loss_stop_pct_of_debit` set, or the runner will only ever
    close on the DTE trigger. The strategy class itself doesn't
    own management — that's `ManagementRules` per the doc 14
    discipline split.

Default parameters per practitioner-canon:

  target_dte_front = 30   (front expiry — fast decay zone)
  target_dte_back = 60    (back expiry — slower decay protection)
  strike_anchor = "atm"   (closest strike to spot at entry)

Recommended ManagementRules for calendars:
  profit_take_pct_of_debit = 0.25  (close at 25% gain on debit)
  loss_stop_pct_of_debit = 0.50    (close at 50% loss of debit)
  dte_close = 7                     (closer to expiry than the
                                      45-DTE-credit-spread default —
                                      front leg is by definition
                                      near expiration)
"""
from __future__ import annotations

from dataclasses import dataclass

from tradegy.options.chain import ChainSnapshot, OptionLeg, OptionSide
from tradegy.options.positions import LegOrder, MultiLegOrder, MultiLegPosition
from tradegy.options.strategies._helpers import (
    is_fillable,
    pick_expiry_closest_to_dte,
)
from tradegy.options.strategy import OptionStrategy


@dataclass(frozen=True)
class PutCalendar30_60AtmDeb(OptionStrategy):
    """ATM put calendar: short 30-DTE put + long 60-DTE put at the
    same (ATM) strike. Net debit position.

    `id` reflects the structure: 30/60 DTE pair, ATM, debit.
    """

    target_dte_front: int = 30
    target_dte_back: int = 60
    contracts: int = 1
    id: str = "put_calendar_30_60_atm_deb"

    def on_chain(
        self,
        snapshot: ChainSnapshot,
        open_positions: tuple[MultiLegPosition, ...],
    ) -> MultiLegOrder | None:
        if self._my_open(open_positions):
            return None

        front = pick_expiry_closest_to_dte(snapshot, self.target_dte_front)
        back = pick_expiry_closest_to_dte(snapshot, self.target_dte_back)
        if front is None or back is None:
            return None
        # Defensive: must be different expiries with back > front.
        if back <= front:
            return None
        front_dte = (front - snapshot.ts_utc.date()).days
        back_dte = (back - snapshot.ts_utc.date()).days
        if front_dte <= 0 or back_dte <= 0:
            return None

        # ATM strike must exist in BOTH expiries' put chains. Pick
        # the strike in front-expiry puts that's closest to spot,
        # then verify that exact strike exists in back-expiry puts.
        front_puts = sorted(
            [
                l for l in snapshot.for_expiry(front)
                if l.side == OptionSide.PUT and is_fillable(l)
            ],
            key=lambda l: l.strike,
        )
        back_puts = {
            l.strike: l
            for l in snapshot.for_expiry(back)
            if l.side == OptionSide.PUT and is_fillable(l)
        }
        if not front_puts or not back_puts:
            return None

        # Pick ATM from front; require the same strike in back.
        # If exact match is missing, walk to the closest-available
        # back-strike (rare — SPX has dense strikes — but defensive).
        front_atm = min(
            front_puts, key=lambda l: abs(l.strike - snapshot.underlying_price),
        )
        if front_atm.strike not in back_puts:
            # Fall back to closest available back strike.
            closest_back_strike = min(
                back_puts.keys(),
                key=lambda k: abs(k - front_atm.strike),
            )
            # Refuse if more than one strike-step away — calendars
            # require same-strike for the term-structure thesis to
            # hold; same-ish defeats the purpose.
            if abs(closest_back_strike - front_atm.strike) > 5.0:
                return None
            atm_strike = closest_back_strike
        else:
            atm_strike = front_atm.strike

        return MultiLegOrder(
            tag=self.id,
            contracts=self.contracts,
            legs=(
                # Short the front (collect fast decay).
                LegOrder(
                    expiry=front, strike=atm_strike,
                    side=OptionSide.PUT, quantity=-1,
                ),
                # Long the back (pay for protection + slow decay).
                LegOrder(
                    expiry=back, strike=atm_strike,
                    side=OptionSide.PUT, quantity=+1,
                ),
            ),
        )
