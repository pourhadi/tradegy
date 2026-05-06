"""Chain-snapshot reader for databento options-on-futures parquet.

Bridges the date-partitioned parquet from `tradegy.ingest.databento_options`
into the typed `ChainSnapshot`/`OptionLeg` dataclasses that strategy
classes consume.  Sibling of `chain_io.py` (which reads ORATS daily
snapshots) — same dataclass output, different input shape.

Key differences from the ORATS reader:

  1. **Sparse minute bars vs full daily snapshots.**  ORATS publishes
     EOD chains where every listed contract has a row.  Databento
     ohlcv-1m only emits a bar for a contract in a minute when that
     contract traded in that minute.  This module exposes two
     iterators to handle both query patterns:

       * `iter_session_chains()` — one ChainSnapshot per session,
         constructed from the LAST bar of each contract that traded
         that day.  Best for strategies that decide entries / exits
         at session boundaries (most 0DTE structures).

       * `iter_minute_snapshots()` — one ChainSnapshot per minute,
         containing only the contracts that traded that minute.
         Best for execution-quality simulation.

  2. **Trade prices, not quotes.**  databento ohlcv-1m carries
     trade-aggregated OHLC, not bid/ask quotes (mbp-1 would, but is
     prohibitively expensive at retail scope).  The reader emits
     `OptionLeg.bid = OptionLeg.ask = bar_close` as a sentinel; mid
     equals close.  Strategies that want a realistic spread MUST
     inflate ask / deflate bid by an explicit slippage at fill
     time.  Recommended: 1-2 ticks per side (MES options tick =
     0.05 = $0.25 per contract).

  3. **No vendor IV.**  databento doesn't publish implied vol; the
     reader emits `OptionLeg.iv = 0.0`.  Strategies that need IV
     must back it out via `tradegy.options.greeks.bs_greeks` IV
     solver from leg price + underlying + risk-free rate + DTE.

  4. **Underlying price not auto-loaded.**  The MES options data
     lists the parent future on each leg (`OptionLeg.underlying`,
     e.g. 'MESM4').  The futures price for that contract at the
     chain timestamp must come from the `mes_1m_ohlcv` source, but
     wiring that join here would couple two sources tightly.
     Instead the reader accepts an optional `underlying_price_lookup`
     callable; if not provided, `ChainSnapshot.underlying_price` is
     emitted as 0.0 sentinel and the caller is responsible for
     injecting it before strategies that need it run.

  5. **Family-level underlying field.**  The ChainSnapshot dataclass
     has one `underlying` slot, but a single date can host options
     on multiple MES futures (MESM4 March, MESH4 quarterly, MESU4
     June).  We set `ChainSnapshot.underlying = "MES"` (the family
     root) and preserve the specific quarterly future on each
     `OptionLeg.underlying`.  Strategies that need one-future-per-
     snapshot semantics can filter the legs by underlying.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

import polars as pl

from tradegy import config
from tradegy.options.chain import (
    ChainSnapshot,
    OptionLeg,
    OptionSide,
)


# MES futures-options multiplier.  Per CME: $5 × S&P 500 index ×
# the option's per-point premium.  i.e. a 1.00 quote on a MES
# option = $5 in cash terms.
_MES_OPTION_MULTIPLIER: int = 5

# Family root used as ChainSnapshot.underlying when the snapshot
# spans multiple quarterly futures.
_FAMILY_ROOT: str = "MES"

# Default risk-free rate when caller doesn't supply one.  2023-2024
# average of the 3-month Treasury yield was ~5%; this is a
# reasonable starting point for IV-extraction purposes when the
# strategy doesn't pass a per-snapshot rate.
_DEFAULT_RISK_FREE_RATE: float = 0.05


UnderlyingPriceLookup = Callable[[datetime, str], float]
"""Signature: (ts_utc, underlying_future_symbol) → underlying price.

If the lookup can't resolve a value, return 0.0.  The reader will
propagate that 0.0 to ChainSnapshot.underlying_price.
"""


def _raw_root(source_id: str, *, root: Path | None = None) -> Path:
    base = root or config.raw_dir()
    return base / f"source={source_id}"


def load_databento_chain_frames(
    source_id: str = "mes_options_chain",
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    root: Path | None = None,
    raw_symbol_prefix: str | None = None,
) -> pl.DataFrame:
    """Read all parquet partitions for a databento options source as
    a single sorted DataFrame.

    `raw_symbol_prefix` filters legs by raw_symbol prefix — useful
    for separating quarterly options ('MES' prefix) from daily
    options ('X1A', 'X2B', etc.).  None means no filter.
    """
    base = _raw_root(source_id, root=root)
    if not base.exists():
        raise FileNotFoundError(
            f"no ingested chain data for source={source_id} at {base}"
        )
    pattern = str(base / "date=*" / "data.parquet")
    df = pl.read_parquet(pattern).sort(["ts_utc", "expiry", "strike", "side"])
    if start is not None:
        df = df.filter(pl.col("ts_utc") >= _to_utc(start))
    if end is not None:
        df = df.filter(pl.col("ts_utc") <= _to_utc(end))
    if raw_symbol_prefix is not None:
        df = df.filter(pl.col("raw_symbol").str.starts_with(raw_symbol_prefix))
    return df


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _row_to_leg(row: dict) -> OptionLeg:
    """Build an OptionLeg from one parquet row.

    bid/ask are both set to the bar close — strategies must apply
    explicit slippage at fill time.  iv is 0.0 (databento has no
    vendor IV).  open_interest is 0 (databento ohlcv-1m doesn't
    carry it; mbp-1 would).
    """
    side = OptionSide.CALL if row["side"] == "call" else OptionSide.PUT
    close = float(row["close"])
    return OptionLeg(
        underlying=str(row["underlying"]),
        expiry=row["expiry"],
        strike=float(row["strike"]),
        side=side,
        bid=close,
        ask=close,
        iv=0.0,
        volume=int(row["volume"]),
        open_interest=0,
        multiplier=_MES_OPTION_MULTIPLIER,
    )


def iter_session_chains(
    source_id: str = "mes_options_chain",
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    root: Path | None = None,
    raw_symbol_prefix: str | None = None,
    risk_free_rate: float = _DEFAULT_RISK_FREE_RATE,
    underlying_price_lookup: UnderlyingPriceLookup | None = None,
) -> Iterator[ChainSnapshot]:
    """Yield one `ChainSnapshot` per trading session, built from the
    LAST minute bar of each contract that traded on that session.

    For 0DTE strategies that decide entries / exits at session
    boundaries this is the natural cadence.  The snapshot's ts_utc
    is the LATEST bar timestamp on that session (close of session
    or end-of-globex-day, whichever is final).

    Within a session, a contract that traded multiple times appears
    once per session (its LAST bar's close becomes the leg's
    bid=ask).  Contracts that didn't trade that session are absent.
    """
    df = load_databento_chain_frames(
        source_id, start=start, end=end, root=root,
        raw_symbol_prefix=raw_symbol_prefix,
    )
    if df.height == 0:
        return

    # For each (session_date, instrument_id), keep only the LAST bar.
    df_with_date = df.with_columns(
        pl.col("ts_utc").dt.date().alias("__session_date"),
    )
    # Sort ascending so .last() in the group gives the chronologically
    # final bar of that contract on that session.
    df_with_date = df_with_date.sort(["__session_date", "instrument_id", "ts_utc"])
    last_per_session = (
        df_with_date.group_by(["__session_date", "instrument_id"], maintain_order=True)
        .agg([
            pl.col("ts_utc").last().alias("ts_utc"),
            pl.col("raw_symbol").last(),
            pl.col("symbol").last(),
            pl.col("underlying").last(),
            pl.col("expiry").last(),
            pl.col("strike").last(),
            pl.col("side").last(),
            pl.col("close").last(),
            pl.col("volume").sum().alias("volume"),
        ])
    ).sort("__session_date")

    # Now group by session_date to produce one ChainSnapshot per day.
    for session_val, group in last_per_session.group_by(
        "__session_date", maintain_order=True
    ):
        session_date = session_val[0]
        # Snapshot ts_utc = the latest bar on that session.
        ts_utc = group["ts_utc"].max()

        # Build legs.
        legs: list[OptionLeg] = []
        for row in group.iter_rows(named=True):
            legs.append(_row_to_leg(row))

        underlying_price = 0.0
        if underlying_price_lookup is not None:
            # Use the most-active future on this session as the
            # snapshot's underlying price proxy.  When multiple
            # quarterly futures host different option expiries on
            # the same session, this picks the one with the most
            # option-bar volume.
            top_und = (
                group.group_by("underlying")
                .agg(pl.col("volume").sum().alias("__vol"))
                .sort("__vol", descending=True)
                .row(0)[0]
            )
            underlying_price = float(underlying_price_lookup(ts_utc, top_und))

        yield ChainSnapshot(
            underlying=_FAMILY_ROOT,
            ts_utc=ts_utc,
            underlying_price=underlying_price,
            risk_free_rate=risk_free_rate,
            legs=tuple(legs),
        )


def iter_minute_snapshots(
    source_id: str = "mes_options_chain",
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    root: Path | None = None,
    raw_symbol_prefix: str | None = None,
    risk_free_rate: float = _DEFAULT_RISK_FREE_RATE,
    underlying_price_lookup: UnderlyingPriceLookup | None = None,
) -> Iterator[ChainSnapshot]:
    """Yield one `ChainSnapshot` per minute, containing only the
    contracts that traded in that minute.

    Sparse — typical minute may have 1-50 legs, vs the 200-1000+
    legs the ORATS daily reader produces.  Use only when execution-
    quality simulation requires intraday granularity; most 0DTE
    strategy decisions happen at session boundaries and are better
    served by `iter_session_chains`.
    """
    df = load_databento_chain_frames(
        source_id, start=start, end=end, root=root,
        raw_symbol_prefix=raw_symbol_prefix,
    )
    if df.height == 0:
        return

    for ts_val, group in df.group_by("ts_utc", maintain_order=True):
        ts_utc = ts_val[0]
        legs = [_row_to_leg(row) for row in group.iter_rows(named=True)]

        underlying_price = 0.0
        if underlying_price_lookup is not None:
            # Per-minute snapshots: pick the underlying with the most
            # volume in this minute.  In sparse minute snapshots there's
            # often exactly one underlying so this is rarely ambiguous.
            top_und = (
                group.group_by("underlying")
                .agg(pl.col("volume").sum().alias("__vol"))
                .sort("__vol", descending=True)
                .row(0)[0]
            )
            underlying_price = float(underlying_price_lookup(ts_utc, top_und))

        yield ChainSnapshot(
            underlying=_FAMILY_ROOT,
            ts_utc=ts_utc,
            underlying_price=underlying_price,
            risk_free_rate=risk_free_rate,
            legs=tuple(legs),
        )


def make_mes_futures_price_lookup(
    futures_source_id: str = "mes_1m_ohlcv",
    *,
    root: Path | None = None,
) -> UnderlyingPriceLookup:
    """Build an `UnderlyingPriceLookup` backed by the front-month
    MES futures parquet (per the project's `mes_1m_ohlcv` source).

    The futures source's roll convention is previous-day-volume; at
    any given session the parquet contains one front-month contract.
    We honor that — when the option leg's underlying is the active
    front-month, we get a price; when it's a back-month contract
    (e.g., December options trading in March), the lookup returns
    0.0 since the front-month source doesn't carry it.

    Memory: read the entire futures frame once and cache for re-
    queries.  At 1m cadence over 2019-2026 it's ~3M rows = ~50MB,
    cheap to keep in memory for the lookup lifetime.
    """
    base = _raw_root(futures_source_id, root=root)
    if not base.exists():
        raise FileNotFoundError(
            f"no ingested futures data for source={futures_source_id} at {base}"
        )
    pattern = str(base / "date=*" / "data.parquet")
    bars = pl.read_parquet(pattern).sort("ts_utc")

    # Build a (ts_utc → close) index for O(log n) lookups via binary
    # search on the sorted ts column.  We only support the
    # front-month present in the source; back-month requests return
    # 0.0 because we don't have those bars.
    ts_arr = bars["ts_utc"].to_list()
    close_arr = bars["close"].to_list()
    symbol_arr = bars["symbol"].to_list()

    # Helper to find the bar at or before a given ts_utc.
    import bisect

    def lookup(ts_utc: datetime, underlying_symbol: str) -> float:
        if not ts_arr:
            return 0.0
        idx = bisect.bisect_right(ts_arr, ts_utc) - 1
        if idx < 0:
            return 0.0
        # Validate that the bar's symbol matches the requested
        # underlying.  When they differ (option leg points at a
        # back-month future not in the front-month source), we
        # return 0.0 rather than the wrong contract's price.
        if symbol_arr[idx] != underlying_symbol:
            # Walk back a few minutes in case of intraday roll —
            # but bound the walk so we don't scan the whole array
            # on a true mismatch.
            for offset in range(1, min(60, idx + 1)):
                if symbol_arr[idx - offset] == underlying_symbol:
                    return float(close_arr[idx - offset])
            return 0.0
        return float(close_arr[idx])

    return lookup
