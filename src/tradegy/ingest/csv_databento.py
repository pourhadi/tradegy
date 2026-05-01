"""Stage 1 — databento OHLCV CSV ingest with front-month rolling.

Databento publishes per-contract OHLCV bars in CSV form: each row carries
its own `symbol` (e.g. `MESM9`, `MESU5`), and at any given timestamp
multiple contracts in the same product family may be present. Backtesting
against this data requires collapsing those rows into a single per-
timestamp series. The collapse decision is the "front-month roll".

Roll method: for each calendar day D (in UTC), the chosen contract is the
one with the highest **previous-day** total volume. This is mechanical
and contains no lookahead — at the 00:00 UTC boundary of day D we already
know day (D-1)'s volume by contract. For the very first day in the file
we use that day's own volume (no prior to consult).

Price levels are NOT back-adjusted. At each roll boundary the absolute
price level can step up or down by the calendar-spread between the old
and new front month. Strategies that operate within a single session
(ORB, intraday VWAP, time-stop momentum) are insulated from this
because their state resets at the session boundary; strategies that key
on absolute price levels across sessions (round-number magnets, fixed
historical thresholds) are not, and inherit the calendar-spread
artifact. This trade-off is documented at the data-source registry
level and is the reason `mes_5s_ohlcv` (Sierra Chart back-adjusted) is
kept alongside any databento source for callers that need continuity.

Schema expected in the CSV (databento OHLCV-1m / OHLCV-1d standard):
    ts_event, rtype, publisher_id, instrument_id, open, high, low,
    close, volume, symbol

Output schema:
    ts_utc (Datetime[ns, UTC]), open, high, low, close, volume,
    symbol, instrument_id

`symbol` and `instrument_id` are preserved so downstream consumers can
verify the chosen front month at audit time.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from tradegy.ingest._common import (
    IngestResult,
    compute_batch_id,
    now_utc_iso,
    sha256_file,
    source_root,
    write_date_partitions,
    write_receipt,
)
from tradegy.types import DataSource


_REQUIRED_CSV_COLS = {
    "ts_event",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "symbol",
}


def ingest_databento_csv(
    csv_path: Path,
    source: DataSource,
    *,
    out_dir: Path | None = None,
) -> IngestResult:
    """Ingest a databento OHLCV CSV with no-lookahead front-month rolling.

    The source's `IngestSpec.format` must be `databento_ohlcv_csv`.
    Output is canonical OHLCV rows with one row per (date, ts_utc) on the
    chosen front-month contract.
    """
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)
    if source.ingest is None or source.ingest.format != "databento_ohlcv_csv":
        raise ValueError(
            f"source {source.id!r} is not declared as databento_ohlcv_csv "
            "(ingest.format mismatch)"
        )

    out_root = source_root(source.id, out_dir=out_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    # Read with explicit dtype so per-symbol price levels parse cleanly.
    raw = pl.read_csv(
        csv_path,
        schema_overrides={
            "ts_event": pl.String,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
            "symbol": pl.String,
            "instrument_id": pl.Int64,
        },
    )
    rows_in = raw.height
    missing = _REQUIRED_CSV_COLS - set(raw.columns)
    if missing:
        raise ValueError(
            f"databento_ohlcv_csv: input missing columns {sorted(missing)}"
        )

    # Databento ts_event is RFC3339 with nanos and trailing Z (UTC). Parse to
    # UTC ns directly — no input_tz needed.
    parsed = raw.with_columns(
        pl.col("ts_event")
        .str.to_datetime(time_unit="ns", time_zone="UTC", strict=True)
        .alias("ts_utc")
    ).drop("ts_event")

    # Drop exact (ts_utc, symbol) duplicates — databento occasionally emits
    # cancel/correct rebroadcasts that match the canonical row.
    parsed = parsed.sort(["ts_utc", "symbol"]).unique(
        subset=["ts_utc", "symbol"], maintain_order=True
    )

    front_month = _resolve_front_month_per_date(parsed)

    rolled = (
        parsed.with_columns(pl.col("ts_utc").dt.date().alias("__date"))
        .join(front_month, on="__date", how="inner")
        .filter(pl.col("symbol") == pl.col("__front_symbol"))
        .drop(["__date", "__front_symbol"])
        .sort("ts_utc")
    )
    if rolled.height == 0:
        raise ValueError(
            f"front-month roll produced zero rows from {csv_path}; "
            "is the input symbol family non-empty?"
        )

    # Keep only the canonical columns (drop databento metadata that doesn't
    # fit the source's declared field schema).
    keep_cols = ["ts_utc", "open", "high", "low", "close", "volume", "symbol"]
    if "instrument_id" in rolled.columns:
        keep_cols.append("instrument_id")
    rolled = rolled.select(keep_cols)
    rows_out = rolled.height
    duplicates_dropped = rows_in - rows_out

    coverage_start = rolled.select(pl.col("ts_utc").min()).item()
    coverage_end = rolled.select(pl.col("ts_utc").max()).item()

    partitions_written = write_date_partitions(rolled, out_root)

    batch_id = compute_batch_id(csv_path, source.id, source.version)
    write_receipt(
        out_root,
        batch_id,
        {
            "source_id": source.id,
            "source_version": source.version,
            "batch_id": batch_id,
            "format": "databento_ohlcv_csv",
            "csv_path": str(csv_path),
            "csv_sha256": sha256_file(csv_path),
            "ingested_at": now_utc_iso(),
            "rows_in": rows_in,
            "rows_out": rows_out,
            "duplicates_dropped": duplicates_dropped,
            "coverage_start": coverage_start.isoformat(),
            "coverage_end": coverage_end.isoformat(),
            "partitions": [str(p) for p in partitions_written],
            "roll_method": "previous_day_volume_max",
        },
    )

    return IngestResult(
        source_id=source.id,
        batch_id=batch_id,
        raw_path=out_root,
        rows_in=rows_in,
        rows_out=rows_out,
        duplicates_dropped=duplicates_dropped,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        partitions_written=partitions_written,
    )


def _resolve_front_month_per_date(df: pl.DataFrame) -> pl.DataFrame:
    """Map each calendar date to its chosen front-month symbol.

    Roll rule: for date D, front month = symbol with highest volume on
    date (D-1). For the earliest date in the file, use D's own volume
    because no prior is available. Ties are broken by lexicographic
    symbol order, which is deterministic but otherwise arbitrary.
    """
    daily_vol = (
        df.with_columns(pl.col("ts_utc").dt.date().alias("__date"))
        .group_by(["__date", "symbol"])
        .agg(pl.col("volume").sum().alias("__day_vol"))
    )

    # For each date, pick the top-volume symbol (current-day reference).
    same_day_top = (
        daily_vol.sort(["__date", "__day_vol", "symbol"], descending=[False, True, False])
        .group_by("__date", maintain_order=True)
        .agg(pl.col("symbol").first().alias("__same_day_front"))
    )

    # Lag the same-day pick by one trading date to obtain the prior-day
    # front-month assignment to apply ON the next date.
    prior_day = same_day_top.sort("__date").with_columns(
        pl.col("__date").shift(-1).alias("__apply_to_date"),
    )

    prior_map = (
        prior_day.drop_nulls("__apply_to_date")
        .select(
            pl.col("__apply_to_date").alias("__date"),
            pl.col("__same_day_front").alias("__front_symbol"),
        )
    )

    # Earliest date has no prior — fall back to its own same-day pick.
    earliest_date = same_day_top.select(pl.col("__date").min()).item()
    earliest_pick = same_day_top.filter(pl.col("__date") == earliest_date).select(
        pl.col("__date"),
        pl.col("__same_day_front").alias("__front_symbol"),
    )

    return pl.concat([earliest_pick, prior_map]).unique(
        subset=["__date"], maintain_order=True
    ).sort("__date")
