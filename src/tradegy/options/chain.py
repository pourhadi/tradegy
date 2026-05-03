"""Options chain snapshot dataclasses.

A `ChainSnapshot` is the unit of per-day options data the harness
consumes — analogous to a `Bar` for the bar-stream pipeline but
covering an entire option chain (every strike, every expiry) at one
moment in time. Vol-selling strategies operate on chain snapshots,
not on tick-by-tick option quotes; daily granularity is sufficient
for the 30-45 DTE positions in scope per `14_options_volatility_
selling.md`.

Schema design notes:

- Strikes + expiries are stored flat, not nested by expiry. The
  flat shape lets polars filter / group / join cleanly. A view
  helper composes a per-expiry projection on demand.
- Bid + ask are stored separately so the harness can simulate
  fills at mid, mid ± offset, or worst-case (ask for buys, bid
  for sells). Vendor-published mid is rejected — we compute it.
- Implied volatility is stored as the vendor reported it (their
  Greeks are not used). When `bs_greeks` is needed we recompute
  from underlying + strike + expiry + IV using our own model.
- `underlying_price` is per-snapshot. Important: this is the
  underlying *at the chain snapshot time*, not a current quote.
  A no-lookahead rule: at time T, the chain snapshot dated T is
  consumable; T+1 is not.

Storage will be parquet under `data/options_chains/<symbol>/
<date>/data.parquet`, keyed by (date, expiry, strike, side). The
ingest module (sibling, not implemented yet — vendor-blocked)
writes; the harness reads.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class OptionSide(str, Enum):
    """Option type. The two values map to standard CALL / PUT;
    string-backed for clean YAML serialization in strategy specs.
    """

    CALL = "call"
    PUT = "put"


@dataclass(frozen=True)
class OptionLeg:
    """One option contract identified by (underlying, expiry, strike,
    side). Quote and IV are populated by the chain ingest; Greeks are
    computed on demand from `bs_greeks` (not stored on the leg, since
    they depend on the snapshot's underlying_price + ts_utc).

    `multiplier` is the contract multiplier — 100 for SPX/SPY equity-
    style options, 50 for /ES futures options. The harness uses it
    when converting per-share premium to per-contract dollars.
    """

    underlying: str
    expiry: date
    strike: float
    side: OptionSide
    bid: float
    ask: float
    iv: float
    volume: int
    open_interest: int
    multiplier: int = 100

    @property
    def mid(self) -> float:
        """Mid-price between bid and ask. Returns NaN-safe 0.0 when
        the quote is locked / one-sided / both-zero — the caller
        decides how to interpret an unfillable leg.
        """
        if self.bid <= 0 and self.ask <= 0:
            return 0.0
        if self.bid <= 0:
            return self.ask
        if self.ask <= 0:
            return self.bid
        return 0.5 * (self.bid + self.ask)


@dataclass(frozen=True)
class ChainSnapshot:
    """All option legs for one underlying at one moment in time.

    `ts_utc` is the snapshot timestamp (typically end-of-day for
    historical chain data; intraday for live). `risk_free_rate` is
    a vendor-supplied or computed scalar used by the Greeks model;
    we store it on the snapshot because rates change over time and
    the historical correctness of a backtest depends on using the
    rate that prevailed at the snapshot date, not today's rate.

    The legs tuple is intentionally unsorted — strategy classes
    that want a specific ordering (by strike, by delta) sort on
    demand. Storing pre-sorted forces a choice the consumer should
    make.
    """

    underlying: str
    ts_utc: datetime
    underlying_price: float
    risk_free_rate: float
    legs: tuple[OptionLeg, ...]

    def expiries(self) -> tuple[date, ...]:
        """Unique expiries on this snapshot, sorted near-to-far."""
        return tuple(sorted({leg.expiry for leg in self.legs}))

    def for_expiry(self, expiry: date) -> tuple[OptionLeg, ...]:
        """All legs at one expiry, sorted by strike then side
        (CALL before PUT at the same strike). Convenient view for
        per-expiry strategies (iron condors, strangles).
        """
        legs = [leg for leg in self.legs if leg.expiry == expiry]
        legs.sort(key=lambda l: (l.strike, l.side.value))
        return tuple(legs)
