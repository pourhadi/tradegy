"""Multi-leg option position model.

A vol-selling backtest tracks groups of legs (iron condors, credit
spreads, strangles, calendars) as single logical "trades" — each
multi-leg position has a net entry credit, an aggregate P&L vs the
current chain snapshot, and a defined max loss that determines
capital reservation.

Quantity convention (shared with the rest of the harness):

    quantity > 0 = long this contract (paid premium)
    quantity < 0 = short this contract (received premium)

Per-leg cash flow at open:

    cost_to_open(per share) = quantity * fill_price

Long leg with qty=+1 and fill=$5.00 → +$5.00 (we paid).
Short leg with qty=-1 and fill=$3.00 → -$3.00 (we received).

Sum across legs gives the net cost-to-open per share. For a credit
spread/condor/strangle this sum is negative (we received net premium);
the negation, multiplied by contract multiplier and number of
contracts, is the entry credit in dollars.

Mark-to-market and closing follow the same convention with current
chain prices as the closing fill.

Per `14_options_volatility_selling.md`: defined-risk only — every
multi-leg position has long protective wings such that max loss is
known and capped at entry. We compute it explicitly so the harness
risk module can reserve capital correctly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

from tradegy.options.chain import (
    ChainSnapshot,
    OptionLeg,
    OptionSide,
)


# ── Single-leg position ────────────────────────────────────────


@dataclass(frozen=True)
class OptionPosition:
    """One leg of a multi-leg trade.

    Frozen — leg state at entry is immutable. Mark-to-market and
    closing produce new objects (e.g., closed-leg records) rather
    than mutating in place. This is the same pattern as the
    bar-driven harness's Fill / Trade dataclasses.

    `contract_id` is a stable string identifier built from
    `(underlying, expiry, strike, side)` — the harness uses it to
    look the leg back up in subsequent chain snapshots for
    mark-to-market.

    `entry_price` is per share (NOT per contract). Multiply by
    `multiplier` for dollar terms.
    """

    contract_id: str
    underlying: str
    expiry: date
    strike: float
    side: OptionSide
    multiplier: int
    quantity: int          # signed: + long, - short
    entry_price: float     # per share, signed by buy/sell
    entry_ts: datetime

    @staticmethod
    def make_contract_id(
        underlying: str, expiry: date, strike: float, side: OptionSide,
    ) -> str:
        """Canonical contract id: e.g. 'SPX_20260220_4500.0_C'."""
        suffix = "C" if side == OptionSide.CALL else "P"
        return f"{underlying}_{expiry.strftime('%Y%m%d')}_{strike}_{suffix}"

    def cost_to_open(self) -> float:
        """Per-share signed cash flow at open. Positive = paid (long
        legs); negative = received (short legs).
        """
        return self.quantity * self.entry_price

    def cost_to_close(self, current_price: float) -> float:
        """Per-share signed cash flow to close at `current_price`.

        Closing reverses the original direction: a long leg sells at
        current price (negative cash flow per share); a short leg
        buys back (positive). The sign is `-quantity * current_price`.
        """
        return -self.quantity * current_price

    def mark_to_market(self, current_price: float) -> float:
        """Per-share unrealized P&L vs entry, signed.

        For a short leg (qty < 0) entered at $5.00 now worth $2.00:
        cost_to_open = -1 * 5 = -5 (received).
        cost_to_close at 2 = -(-1) * 2 = +2 (would pay back).
        net = -(cost_to_open + cost_to_close) = -(-5 + 2) = +3.
        We received 5, would pay 2, profit 3 — correct.
        """
        return -(self.cost_to_open() + self.cost_to_close(current_price))


# ── Multi-leg position ─────────────────────────────────────────


@dataclass(frozen=True)
class MultiLegPosition:
    """Group of OptionPosition legs comprising one logical trade.

    Iron condors, credit spreads, strangles, and calendars are all
    instances of this — the structure is which legs are long vs
    short, which strikes are wings vs body. The harness treats
    each MultiLegPosition as a single decision unit: open or
    closed, full or none.

    `contracts` is the multiplier on the leg quantities — a 5-lot
    iron condor has contracts=5 and each leg's quantity is ±1
    (representing the "per lot" structure). Total dollar exposure
    is `quantity * contracts * multiplier * price`.

    `max_loss` is the per-lot defined-risk capital reservation in
    dollars. For a 1-lot iron condor with $50 entry credit and
    $5 wing widths: max loss = (5 - 0.50) * 100 = $450. The
    harness's risk module sums max_loss * contracts across open
    positions to compute total capital at risk.
    """

    position_id: str
    strategy_class: str
    contracts: int
    legs: tuple[OptionPosition, ...]
    entry_ts: datetime
    entry_credit_per_share: float   # net per-share, positive = credit, negative = debit
    max_loss_per_contract: float    # dollars at risk per contract
    open: bool = True
    closed_ts: datetime | None = None
    closed_pnl_per_share: float | None = None
    closed_reason: str = ""

    @property
    def underlyings(self) -> tuple[str, ...]:
        return tuple({l.underlying for l in self.legs})

    @property
    def expiries(self) -> tuple[date, ...]:
        return tuple(sorted({l.expiry for l in self.legs}))

    @property
    def total_capital_at_risk(self) -> float:
        """Per-position dollar capital reserved (max_loss × contracts)."""
        return self.max_loss_per_contract * self.contracts

    @property
    def entry_credit_dollars(self) -> float:
        """Total credit received (positive) or debit paid (negative)
        in dollars at open.
        """
        if not self.legs:
            return 0.0
        # All legs in a multi-leg should share the same multiplier.
        mult = self.legs[0].multiplier
        return self.entry_credit_per_share * mult * self.contracts

    def days_to_expiry(self, ts_utc: datetime) -> int:
        """Calendar days from `ts_utc` to the NEAREST expiry across
        all legs. For management triggers — the 21-DTE rule fires on
        whichever side of a calendar/diagonal expires first.
        """
        nearest = min(self.expiries)
        return (nearest - ts_utc.date()).days

    def mark_to_market(self, snap: ChainSnapshot) -> float:
        """Per-share signed unrealized P&L using `snap`'s leg prices
        as the close mark.

        Looks each leg up in the snapshot by (expiry, strike, side).
        Missing legs (the contract no longer trades, or sparse chain)
        are marked at intrinsic — for a closed-out chain that's the
        right floor; for sparse-chain backtest segments it surfaces
        as a clearly-defined assumption rather than silently
        producing NaN.
        """
        total_per_share = 0.0
        for leg in self.legs:
            mark = _lookup_mark(snap, leg)
            total_per_share += leg.mark_to_market(mark)
        return total_per_share

    def mark_dollars(self, snap: ChainSnapshot) -> float:
        """Total unrealized P&L in dollars at this snapshot."""
        if not self.legs:
            return 0.0
        mult = self.legs[0].multiplier
        return self.mark_to_market(snap) * mult * self.contracts

    def pnl_pct_of_max_credit(self, snap: ChainSnapshot) -> float:
        """Unrealized P&L as fraction of max possible profit (the
        entry credit, for credit positions).

        Returns NaN for debit positions or zero-credit positions
        (calendar spreads can be either; the management trigger uses
        a different metric for those).
        """
        if self.entry_credit_per_share <= 0:
            return float("nan")
        return self.mark_to_market(snap) / self.entry_credit_per_share

    def pnl_pct_of_debit(self, snap: ChainSnapshot) -> float:
        """Unrealized P&L as fraction of the debit paid at entry.

        For debit positions (calendar spreads, long verticals):
        entry_credit_per_share is negative; debit_per_share =
        -entry_credit_per_share. PnL pct = mark / debit. Positive
        when the position is profitable.

        Returns NaN for credit positions (the inverse trigger lives
        on pnl_pct_of_max_credit).
        """
        debit = -self.entry_credit_per_share
        if debit <= 0:
            return float("nan")
        return self.mark_to_market(snap) / debit


# ── Multi-leg construction helpers ─────────────────────────────


@dataclass(frozen=True)
class LegOrder:
    """Specification of a single leg to fill at the next snapshot
    open. Strategy classes return collections of these wrapped in
    a MultiLegOrder; the harness fills them and constructs the
    resulting MultiLegPosition.
    """

    expiry: date
    strike: float
    side: OptionSide
    quantity: int   # signed: + long, - short


@dataclass(frozen=True)
class MultiLegOrder:
    """Submission from a strategy to open a new multi-leg position.

    `tag` is a human-readable string for the audit trail
    ('iron_condor_45dte_d16', 'put_credit_spread', etc.).
    """

    tag: str
    contracts: int
    legs: tuple[LegOrder, ...]


# ── Defined-risk math ──────────────────────────────────────────


def compute_max_loss_per_contract(
    legs: Iterable[OptionPosition], entry_credit_per_share: float,
) -> float:
    """Compute defined-risk max loss for a multi-leg position.

    Algorithm: at each strike level on the underlying continuum,
    evaluate the multi-leg payoff at expiry. The most-negative
    point is the worst-case loss. Subtract the entry credit
    (positive credit reduces loss) to get net max loss in dollars
    per contract.

    For an iron condor with wings W and credit C:
        max_loss = (W - C) * multiplier
    For a put credit spread with width W and credit C:
        max_loss = (W - C) * multiplier
    For a calendar (single strike, different expiries): undefined
    until the near leg expires; we conservatively use a floor of
    the long-leg debit (the back month protects against the front
    going to ∞).

    This generic implementation handles all cases by sampling the
    payoff curve. Sample range covers from 0 to 2×max_strike so we
    catch the upside tail.
    """
    leg_list = list(legs)
    if not leg_list:
        return 0.0
    mult = leg_list[0].multiplier

    strikes = [l.strike for l in leg_list]
    if not strikes:
        return 0.0
    max_k = max(strikes)
    # Sample the payoff at every strike + just-below + just-above
    # each strike + the wings (0 and 2*max_k). Per-share payoff at
    # underlying value U for a leg at expiry:
    #   long call:  qty * max(U - K, 0)   (qty=+|n|)
    #   short call: qty * max(U - K, 0)   (qty=-|n|)
    #   long put:   qty * max(K - U, 0)
    #   short put:  qty * max(K - U, 0)
    sample_points: set[float] = {0.0, 2.0 * max_k}
    for k in strikes:
        sample_points.add(k)
        sample_points.add(k - 0.01)
        sample_points.add(k + 0.01)

    worst_payoff = 0.0
    for U in sample_points:
        payoff_per_share = 0.0
        for leg in leg_list:
            if leg.side == OptionSide.CALL:
                intrinsic = max(U - leg.strike, 0.0)
            else:
                intrinsic = max(leg.strike - U, 0.0)
            payoff_per_share += leg.quantity * intrinsic
        if payoff_per_share < worst_payoff:
            worst_payoff = payoff_per_share

    # Net P&L at worst point = entry credit (received) + worst
    # payoff (negative). Max loss = -(net P&L), in dollars per
    # contract.
    net_per_share = entry_credit_per_share + worst_payoff
    max_loss_per_share = -net_per_share
    return max(0.0, max_loss_per_share * mult)


# ── Internals ──────────────────────────────────────────────────


def _lookup_mark(snap: ChainSnapshot, leg: OptionPosition) -> float:
    """Look up the per-share mark price for `leg` in `snap`.

    Convention: closing a long leg sells at bid (worst-case for
    seller), closing a short leg buys at ask. We use mid as the
    default mark — strategy classes can override with bid/ask if
    they want worst-case marking. For sparse / missing legs falls
    back to intrinsic value at the snapshot's underlying.
    """
    for chain_leg in snap.for_expiry(leg.expiry):
        if (
            chain_leg.strike == leg.strike
            and chain_leg.side == leg.side
        ):
            mid = chain_leg.mid
            if mid > 0:
                return mid
            # Quote dropped — fall through to intrinsic.
            break
    if leg.side == OptionSide.CALL:
        return max(snap.underlying_price - leg.strike, 0.0)
    return max(leg.strike - snap.underlying_price, 0.0)
