"""0DTE-specific backtest harness for databento options chains.

Why a separate harness.  The existing `tradegy.options.runner` is
designed for multi-day vol-selling positions managed via the
universal 50/21/200 rule (close at 50% profit / 21 DTE / 200% loss).
0DTE positions don't fit that model: they're opened and closed in
the same session, and the dte_close=21 trigger fires immediately
at entry (DTE = 0).  Bolting 0DTE semantics onto that runner would
require either gutting the management rules or carrying around a
parallel "intraday" code path; cleaner to build a focused harness.

What it does.  For each 0DTE-eligible session date in the data:

  1. Find the chain at TIME = `entry_time_ut_minutes_from_open` after
     globex open.  Default 60 min ≈ 10:30 ET — past the first-30m
     vol shake-out, plenty of premium left.
  2. Call `strategy.on_chain(snapshot, ())` to get a MultiLegOrder.
  3. Compute entry credit from the leg close prices in the snapshot.
  4. Look up the underlying-future settlement price at session close
     (last bar of the day in `mes_1m_ohlcv`).
  5. Settle each leg to intrinsic value vs that settlement price.
     For 0DTE this is exact at expiration: the leg pays
     max(0, S - K) for calls, max(0, K - S) for puts.
  6. Compute net P&L = entry_credit - sum(intrinsic close costs).
  7. Apply slippage and commissions.
  8. Aggregate.

Settlement choice.  Intrinsic-at-close is the right model for 0DTE:
    - At the bell on expiry day the option settles to its intrinsic
      value vs the future's settlement print.
    - Using the last MARKET bar of each leg as the close price is
      noisier (illiquid strikes have stale bars) and not how
      settlement actually works.
    - Using bid/ask quotes would require mbp-1 (~$1.5K for 5yr); we
      have ohlcv-1m only.  Intrinsic is the analytically-correct
      cash-equivalent.

What this harness deliberately does NOT do (the runner has these,
0DTE doesn't need them or they don't apply):

  - Multi-day position management
  - 50% profit-take / 21-DTE close / 200% loss-stop triggers
  - Mark-to-market reporting between snapshots (only entry +
    settlement matter for 0DTE backtest)
  - Concentration / capital-cap risk gates (single-position-per-
    session is the backtest's by-construction concentration)

Cost model defaults match the project convention:
    - slippage_per_leg_dollars: $0.25 per side per leg ($1 per round-
      trip per leg = 1 tick on MES options at $0.05 per tick × $5
      multiplier).
    - commission_per_leg_dollars: $1.50 round-trip per leg.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterator

import polars as pl

from tradegy.options.chain import ChainSnapshot, OptionLeg, OptionSide
from tradegy.options.databento_chain_io import (
    UnderlyingPriceLookup,
    _row_to_leg,
    load_databento_chain_frames,
    make_mes_futures_price_lookup,
)
from tradegy.options.strategy import OptionStrategy


# Default cost parameters per side per leg.
_DEFAULT_SLIPPAGE_PER_LEG: float = 0.25     # dollars
_DEFAULT_COMMISSION_PER_LEG_RT: float = 1.50  # dollars round-trip

# Default entry time: 60 minutes after the start of regular trading
# hours (RTH, 9:30 ET = 14:30 UTC).  10:30 ET = 14:30 UTC, after the
# first 30-min vol shake-out.
_DEFAULT_ENTRY_TIME_UTC: time = time(14, 30)

# Default settlement time: end of regular trading hours (16:00 ET =
# 20:00 UTC).  At this point the 0DTE option settles to intrinsic.
_DEFAULT_SETTLEMENT_TIME_UTC: time = time(20, 0)


@dataclass(frozen=True)
class TradeRecord:
    """One 0DTE round-trip trade outcome.

    `close_reason` is "settlement" by default (held to expiry, intrinsic
    cash settlement) or one of "profit_take" / "loss_stop" when intraday
    management triggers fired.

    `close_ts` and `close_underlying` reflect the actual close — for
    settlement they equal `settlement_ts` and `underlying_at_settlement`;
    for early management they may be hours earlier and at a different
    underlying price.
    """

    session_date: date
    entry_ts: datetime
    settlement_ts: datetime
    underlying_at_entry: float
    underlying_at_settlement: float
    n_legs: int
    contracts: int
    entry_credit_per_share: float    # signed: + credit received, - debit paid
    settlement_intrinsic_per_share: float  # cost to close at the actual close (intrinsic OR market)
    pnl_per_share_gross: float       # entry_credit - settlement_intrinsic
    pnl_dollars_gross: float         # × multiplier × contracts
    slippage_dollars: float
    commission_dollars: float
    pnl_dollars_net: float           # gross - slippage - commission
    leg_strikes: tuple[float, ...]
    leg_sides: tuple[str, ...]
    leg_quantities: tuple[int, ...]
    close_reason: str = "settlement"     # one of: settlement, profit_take, loss_stop
    close_ts: datetime | None = None
    close_underlying: float = 0.0
    notes: str = ""


@dataclass(frozen=True)
class BacktestResult:
    """Aggregate result over a 0DTE backtest run."""

    n_sessions_total: int
    n_sessions_traded: int
    n_sessions_skipped_no_entry: int
    trades: tuple[TradeRecord, ...]

    @property
    def total_pnl_gross(self) -> float:
        return sum(t.pnl_dollars_gross for t in self.trades)

    @property
    def total_pnl_net(self) -> float:
        return sum(t.pnl_dollars_net for t in self.trades)

    @property
    def n_winners(self) -> int:
        return sum(1 for t in self.trades if t.pnl_dollars_net > 0)

    @property
    def n_losers(self) -> int:
        return sum(1 for t in self.trades if t.pnl_dollars_net < 0)

    @property
    def win_rate(self) -> float:
        n = len(self.trades)
        return self.n_winners / n if n > 0 else 0.0

    @property
    def avg_pnl_net(self) -> float:
        n = len(self.trades)
        return self.total_pnl_net / n if n > 0 else 0.0


def _session_dates_with_zero_dte(
    df: pl.DataFrame,
) -> list[date]:
    """Return the sorted list of session dates that have at least
    one same-day expiring contract in the data.
    """
    sd = (
        df.with_columns(pl.col("ts_utc").dt.date().alias("__bar_date"))
        .filter(pl.col("__bar_date") == pl.col("expiry"))
        .select("__bar_date")
        .unique()
        .sort("__bar_date")
    )
    return sd["__bar_date"].to_list()


def _snapshot_at_time(
    df: pl.DataFrame,
    target_ts: datetime,
    risk_free_rate: float,
    underlying_price_lookup: UnderlyingPriceLookup | None,
) -> ChainSnapshot | None:
    """Build a ChainSnapshot from `df` containing the LAST bar of
    each contract that traded on-or-before `target_ts`.

    `df` should already be filtered to the relevant session date.
    """
    eligible = df.filter(pl.col("ts_utc") <= target_ts)
    if eligible.height == 0:
        return None

    # For each (instrument_id), keep only the last bar at/before target.
    last_per_contract = (
        eligible.sort(["instrument_id", "ts_utc"])
        .group_by("instrument_id", maintain_order=True)
        .agg([
            pl.col("ts_utc").last().alias("ts_utc"),
            pl.col("symbol").last(),
            pl.col("raw_symbol").last(),
            pl.col("underlying").last(),
            pl.col("expiry").last(),
            pl.col("strike").last(),
            pl.col("side").last(),
            pl.col("close").last(),
            pl.col("volume").sum().alias("volume"),
        ])
    )

    legs = [_row_to_leg(row) for row in last_per_contract.iter_rows(named=True)]

    underlying_price = 0.0
    most_active_underlying = None
    if underlying_price_lookup is not None and last_per_contract.height > 0:
        most_active_underlying = (
            last_per_contract.group_by("underlying")
            .agg(pl.col("volume").sum().alias("__vol"))
            .sort("__vol", descending=True)
            .row(0)[0]
        )
        underlying_price = float(
            underlying_price_lookup(target_ts, most_active_underlying)
        )

    return ChainSnapshot(
        underlying="MES",
        ts_utc=target_ts,
        underlying_price=underlying_price,
        risk_free_rate=risk_free_rate,
        legs=tuple(legs),
    )


def _settle_position_intrinsic(
    order_legs: tuple,
    underlying_at_settlement: float,
) -> float:
    """Compute total intrinsic close cost per-share for a position.

    Per leg, the close cost = -quantity × intrinsic_value.
    For a short call (qty=-1) ending ITM at S=110, K=100: intrinsic =
    10, close_cost = -(-1) × 10 = +10 (we pay $10 to buy back the call
    that's worth $10).  For a long call same situation: close_cost =
    -(+1) × 10 = -10 (we receive $10 selling the long call).  Total
    cost is the sum across legs.
    """
    total = 0.0
    for leg in order_legs:
        K = leg.strike
        if leg.side == OptionSide.CALL:
            intrinsic = max(0.0, underlying_at_settlement - K)
        else:
            intrinsic = max(0.0, K - underlying_at_settlement)
        # close_cost = -quantity × intrinsic
        total += -leg.quantity * intrinsic
    return total


def _entry_credit_per_share(
    order_legs: tuple,
    snapshot: ChainSnapshot,
) -> float:
    """Compute entry credit per share from leg close prices in the
    snapshot.

    Per leg, signed cash flow at entry = -quantity × close_price.
    Selling a call (qty=-1) at $5: -(-1) × 5 = +5 (received).
    Buying a long-wing put at $1: -(+1) × 1 = -1 (paid).
    Sum is the net entry credit (positive if net cash received).
    """
    total = 0.0
    for order_leg in order_legs:
        chain_leg = _find_chain_leg(snapshot, order_leg)
        if chain_leg is None:
            # Strategy emitted a leg not in the snapshot — should
            # be impossible given strategy reads from the snapshot.
            return float("nan")
        # Use bar close as both bid and ask (chain reader sentinel).
        # Slippage is accounted for separately.
        price = chain_leg.bid
        total += -order_leg.quantity * price
    return total


def _find_chain_leg(
    snapshot: ChainSnapshot,
    order_leg,
) -> OptionLeg | None:
    """Locate the matching OptionLeg in the snapshot by (expiry,
    strike, side).
    """
    for cl in snapshot.legs:
        if (
            cl.expiry == order_leg.expiry
            and cl.strike == order_leg.strike
            and cl.side == order_leg.side
        ):
            return cl
    return None


def _close_cost_per_share_at_ts(
    sess_df: pl.DataFrame,
    order_legs: tuple,
    check_ts: datetime,
    fallback_underlying_price: float | None = None,
) -> float | None:
    """Compute the cost-to-close-per-share at `check_ts` from the
    most recent bars of each leg.

    Per leg, close_cost = -quantity × leg_price.  We use each leg's
    most recent bar at-or-before check_ts as its mark.  If a leg
    has no bar yet (didn't trade between session open and check_ts)
    AND no fallback_underlying_price, returns None — caller skips.

    If `fallback_underlying_price` is provided, missing legs mark
    to intrinsic vs that price.  This is only used at settlement;
    intraday checks should pass None and let the caller skip the
    interval.
    """
    total = 0.0
    for leg in order_legs:
        # Filter sess_df to bars for this contract at-or-before check_ts.
        leg_bars = sess_df.filter(
            (pl.col("strike") == leg.strike)
            & (pl.col("side") == leg.side.value)
            & (pl.col("expiry") == leg.expiry)
            & (pl.col("ts_utc") <= check_ts)
        )
        if leg_bars.height > 0:
            mark = float(leg_bars.tail(1)["close"].item())
        elif fallback_underlying_price is not None:
            if leg.side == OptionSide.CALL:
                mark = max(0.0, fallback_underlying_price - leg.strike)
            else:
                mark = max(0.0, leg.strike - fallback_underlying_price)
        else:
            return None
        total += -leg.quantity * mark
    return total


def _intraday_check_intervals(
    entry_ts: datetime, settlement_ts: datetime, interval_minutes: int = 15,
) -> list[datetime]:
    """Generate timestamps from entry+interval up to settlement
    (exclusive) at `interval_minutes` cadence.
    """
    out: list[datetime] = []
    cur = entry_ts + timedelta(minutes=interval_minutes)
    while cur < settlement_ts:
        out.append(cur)
        cur += timedelta(minutes=interval_minutes)
    return out


def run_zero_dte_backtest(
    strategy: OptionStrategy,
    *,
    source_id: str = "mes_options_chain",
    start: datetime | None = None,
    end: datetime | None = None,
    root: Path | None = None,
    raw_symbol_prefix: str | None = None,
    entry_time_utc: time = _DEFAULT_ENTRY_TIME_UTC,
    settlement_time_utc: time = _DEFAULT_SETTLEMENT_TIME_UTC,
    risk_free_rate: float = 0.05,
    underlying_price_lookup: UnderlyingPriceLookup | None = None,
    slippage_per_leg_dollars: float = _DEFAULT_SLIPPAGE_PER_LEG,
    commission_per_leg_round_trip: float = _DEFAULT_COMMISSION_PER_LEG_RT,
    profit_take_pct: float | None = None,
    loss_stop_pct: float | None = None,
    intraday_check_interval_minutes: int = 15,
) -> BacktestResult:
    """Run a 0DTE backtest of `strategy` over the given window.

    See module docstring for the per-session lifecycle.

    `underlying_price_lookup` is required: the strategy needs a
    populated underlying_price to anchor strike selection, and the
    settlement step needs the close price for intrinsic settlement.
    Pass `make_mes_futures_price_lookup()` for the standard mes_1m
    futures-bar lookup.
    """
    if underlying_price_lookup is None:
        raise ValueError(
            "underlying_price_lookup is required — strategy strike "
            "selection and settlement both depend on it.  Pass "
            "make_mes_futures_price_lookup() for the default lookup "
            "backed by the mes_1m_ohlcv source."
        )

    # Load all bars in the window once.
    df_all = load_databento_chain_frames(
        source_id, start=start, end=end, root=root,
        raw_symbol_prefix=raw_symbol_prefix,
    )
    if df_all.height == 0:
        return BacktestResult(
            n_sessions_total=0,
            n_sessions_traded=0,
            n_sessions_skipped_no_entry=0,
            trades=tuple(),
        )

    session_dates = _session_dates_with_zero_dte(df_all)
    trades: list[TradeRecord] = []
    n_skipped = 0

    df_with_date = df_all.with_columns(
        pl.col("ts_utc").dt.date().alias("__bar_date"),
    )

    for sess_date in session_dates:
        # Build the entry-time + settlement-time targets in UTC.
        entry_ts = datetime.combine(sess_date, entry_time_utc, tzinfo=timezone.utc)
        settlement_ts = datetime.combine(
            sess_date, settlement_time_utc, tzinfo=timezone.utc,
        )

        sess_df = df_with_date.filter(pl.col("__bar_date") == sess_date)
        # Build entry snapshot.
        entry_snap = _snapshot_at_time(
            sess_df, entry_ts, risk_free_rate, underlying_price_lookup,
        )
        if entry_snap is None or entry_snap.underlying_price <= 0:
            n_skipped += 1
            continue

        # Strategy decision.
        order = strategy.on_chain(entry_snap, open_positions=())
        if order is None:
            n_skipped += 1
            continue

        # Entry credit from leg prices in the entry snapshot.
        ec = _entry_credit_per_share(order.legs, entry_snap)
        if ec != ec:  # NaN — strategy picked a leg not in snapshot
            n_skipped += 1
            continue

        # Settlement underlying — use the underlying lookup at settle ts,
        # picking the same underlying future the strategy entered against.
        # We get it from any leg in the order (they all share underlying
        # for a single-expiry IC).  If the leg's underlying is in the
        # snapshot, use that; else use the most-active underlying lookup.
        target_underlying = entry_snap.legs[0].underlying if entry_snap.legs else "MESM4"
        # Walk back from settlement_ts to find the latest available
        # MES future close at-or-before that time.  The
        # underlying_price_lookup we built is timestamp-keyed binary-
        # search in the futures source; it returns 0 if no bar found.
        S_settle = float(underlying_price_lookup(settlement_ts, target_underlying))
        if S_settle <= 0:
            n_skipped += 1
            continue

        # Intraday management: check at periodic intervals between
        # entry and settlement.  Trigger profit-take if MTM ≥
        # profit_take_pct of credit; loss-stop if MTM ≤
        # -loss_stop_pct of credit.  First triggering interval wins;
        # otherwise fall through to settlement.
        close_reason = "settlement"
        close_ts: datetime | None = None
        close_underlying = 0.0
        close_cost_at_close: float | None = None

        if (profit_take_pct is not None or loss_stop_pct is not None) and ec > 0:
            for check_ts in _intraday_check_intervals(
                entry_ts, settlement_ts, intraday_check_interval_minutes,
            ):
                cc = _close_cost_per_share_at_ts(sess_df, order.legs, check_ts)
                if cc is None:
                    continue  # not all legs have a bar yet
                # Per-share PnL at this check.
                pnl_chk = ec - cc
                # Convert to fraction of entry credit.
                pnl_frac = pnl_chk / ec
                if profit_take_pct is not None and pnl_frac >= profit_take_pct:
                    close_reason = "profit_take"
                    close_ts = check_ts
                    close_underlying = float(
                        underlying_price_lookup(check_ts, target_underlying)
                    )
                    close_cost_at_close = cc
                    break
                if loss_stop_pct is not None and pnl_frac <= -loss_stop_pct:
                    close_reason = "loss_stop"
                    close_ts = check_ts
                    close_underlying = float(
                        underlying_price_lookup(check_ts, target_underlying)
                    )
                    close_cost_at_close = cc
                    break

        # Determine settlement_cost: for early management it's the
        # close_cost at the trigger time; for held-to-settlement it's
        # intrinsic at settlement_ts.
        if close_cost_at_close is not None:
            settlement_cost = close_cost_at_close
        else:
            settlement_cost = _settle_position_intrinsic(order.legs, S_settle)
            close_ts = settlement_ts
            close_underlying = S_settle

        pnl_per_share = ec - settlement_cost
        # MES options multiplier is $5 per point.
        multiplier = entry_snap.legs[0].multiplier if entry_snap.legs else 5
        pnl_gross = pnl_per_share * multiplier * order.contracts

        # Cost: slippage on each leg per side (entry + close), commission
        # round-trip per leg.
        n_legs = len(order.legs)
        slippage = 2 * n_legs * slippage_per_leg_dollars * order.contracts
        commission = n_legs * commission_per_leg_round_trip * order.contracts
        pnl_net = pnl_gross - slippage - commission

        trades.append(TradeRecord(
            session_date=sess_date,
            entry_ts=entry_ts,
            settlement_ts=settlement_ts,
            underlying_at_entry=entry_snap.underlying_price,
            underlying_at_settlement=S_settle,
            n_legs=n_legs,
            contracts=order.contracts,
            entry_credit_per_share=ec,
            settlement_intrinsic_per_share=settlement_cost,
            pnl_per_share_gross=pnl_per_share,
            pnl_dollars_gross=pnl_gross,
            slippage_dollars=slippage,
            commission_dollars=commission,
            pnl_dollars_net=pnl_net,
            leg_strikes=tuple(l.strike for l in order.legs),
            leg_sides=tuple(l.side.value for l in order.legs),
            leg_quantities=tuple(l.quantity for l in order.legs),
            close_reason=close_reason,
            close_ts=close_ts,
            close_underlying=close_underlying,
        ))

    return BacktestResult(
        n_sessions_total=len(session_dates),
        n_sessions_traded=len(trades),
        n_sessions_skipped_no_entry=n_skipped,
        trades=tuple(trades),
    )
