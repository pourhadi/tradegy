"""Stage 1 — databento options-on-futures CSV ingest.

Databento's `GLBX.MDP3` dataset publishes futures options as two
parallel CSV streams that must be joined to be useful:

  * `definition` schema  → one row per (instrument_id, update event)
                           with raw_symbol / expiration / strike_price /
                           instrument_class (C/P) / underlying.
  * `ohlcv-1m` schema    → per-minute OHLCV bars per instrument_id
                           (sparse — only emitted for minutes with
                           trade activity).

The join key is `instrument_id`. For a 0DTE-grade backtest we only
need to know the contract's strike, expiry, side, and underlying, plus
the per-minute OHLC and volume.  This module turns the raw CSV pair
into the project's canonical date-partitioned parquet layout under
`data/raw/source=<id>/date=YYYY-MM-DD/data.parquet`.

CME MES options are addressed via two separate parent-symbology trees,
both of which we admit here:

  * `MES.OPT`         → standard quarterlies (3rd-Friday Mar/Jun/Sep/
                         Dec).  Useless for 0DTE on its own.
  * `X[1-5][A-D].OPT` → daily MES options.  Letter = day of week
                         (A=Mon, B=Tue, C=Wed, D=Thu).  Number =
                         n-th occurrence of that weekday in the
                         listing month.  20 parents total.

This ingest runs once per (parent, schema) CSV pair; the caller can
union as many pairs as desired into a single source.  The downstream
chain reader sees one logical chain per (date, expiry, strike, side).

Important schema notes:

  * databento ohlcv-1m carries TRADE prices only — no bid/ask.  Per-
    leg quotes require the `mbp-1` schema (~$1.5K for 5yr).  Strategy
    classes that consume this output must approximate bid/ask from
    the bar close + a slippage assumption (per-tick = $0.05 = 25 cents
    on a $5 multiplier MES option).  The chain reader produces
    OptionLeg objects with bid = close, ask = close as a sentinel —
    strategies that need realistic spreads MUST inflate ask and
    deflate bid by the configured slippage at fill time.
  * Definition rows are emitted whenever a contract's metadata is
    updated (security_update_action) — many rows per logical contract.
    This module deduplicates by `instrument_id`, taking the first row
    encountered (strike/expiry/side never change after listing).
  * Multi-leg user-defined contracts (instrument_class='T'/'M') are
    excluded — only outright C and P contracts are emitted.

The output schema is intentionally NOT the same shape as the ORATS
chain ingest (`csv_orats.py`), because:

  * ORATS rows are per-(date, expiry, strike) with both call and put
    side-by-side (since ORATS publishes EOD chains where the universe
    is known and complete).
  * Databento rows are per-(timestamp, instrument_id) with a single
    leg, since not every contract trades every minute.

A separate chain-reader module (`databento_chain_io.py`) projects this
flat per-leg time-series into per-snapshot ChainSnapshot objects.
"""
from __future__ import annotations

from datetime import datetime
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


# Definition-schema columns we actually consume.  Anything else in the
# 74-column databento definition stream is dropped on ingest.
_DEF_COLS_NEEDED = (
    "instrument_id",
    "raw_symbol",
    "instrument_class",   # 'C' (call), 'P' (put), 'T' (multi-leg), 'M' (other)
    "expiration",         # ISO datetime with trailing Z; parse to Date
    "strike_price",
    "underlying",         # e.g. 'MESM4'
)

# OHLCV-schema columns we actually consume.
_OHLCV_COLS_NEEDED = (
    "ts_event",
    "instrument_id",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
)


def _parse_definitions(def_csv: Path) -> pl.DataFrame:
    """Read a databento definition CSV and reduce to one row per
    instrument_id with strike/expiry/side/underlying.

    We take the FIRST row per instrument_id because the metadata
    fields we use (strike, expiry, instrument_class, underlying) are
    invariant after contract listing.  Update/correction rows
    (security_update_action != 'A') still carry the same metadata but
    we only need one copy.
    """
    df = pl.read_csv(
        def_csv,
        infer_schema_length=10000,
        ignore_errors=True,
    )
    missing = [c for c in _DEF_COLS_NEEDED if c not in df.columns]
    if missing:
        raise ValueError(
            f"definition CSV {def_csv.name} missing required columns "
            f"{missing}; columns present: {df.columns}"
        )
    df = df.select(list(_DEF_COLS_NEEDED))

    # Filter to outright C/P only — drop multi-leg/spread definitions.
    df = df.filter(pl.col("instrument_class").is_in(["C", "P"]))

    # Parse expiration ISO datetime (with trailing Z) to a Date.
    df = df.with_columns(
        pl.col("expiration")
        .str.replace(r"\.\d+Z$", "")
        .str.strptime(pl.Datetime, "%Y-%m-%dT%H:%M:%S", strict=False)
        .dt.date()
        .alias("expiry")
    ).drop("expiration")

    # Map 'C' / 'P' → 'call' / 'put' (matches OptionSide enum).
    df = df.with_columns(
        pl.when(pl.col("instrument_class") == "C")
        .then(pl.lit("call"))
        .otherwise(pl.lit("put"))
        .alias("side")
    ).drop("instrument_class")

    # Cast strike for downstream consistency.
    df = df.with_columns(pl.col("strike_price").cast(pl.Float64).alias("strike")).drop(
        "strike_price"
    )

    # Dedupe — take first occurrence per instrument_id.
    df = df.unique(subset=["instrument_id"], keep="first", maintain_order=True)

    return df.select(["instrument_id", "raw_symbol", "underlying", "expiry", "strike", "side"])


def _parse_ohlcv(bars_csv: Path) -> pl.DataFrame:
    """Read a databento ohlcv-1m CSV and parse ts_event to UTC ns."""
    df = pl.read_csv(
        bars_csv,
        infer_schema_length=10000,
        ignore_errors=True,
    )
    missing = [c for c in _OHLCV_COLS_NEEDED if c not in df.columns]
    if missing:
        raise ValueError(
            f"ohlcv-1m CSV {bars_csv.name} missing required columns "
            f"{missing}; columns present: {df.columns}"
        )
    df = df.select(list(_OHLCV_COLS_NEEDED))

    # Databento ts_event is RFC3339 with trailing Z (UTC) and full
    # nanosecond precision.  Strip the trailing Z + nanos portion to
    # get a clean format polars can parse without a tz-aware schema
    # mismatch.
    df = df.with_columns(
        pl.col("ts_event")
        .str.replace(r"\.\d+Z$", "")
        .str.strptime(pl.Datetime, "%Y-%m-%dT%H:%M:%S", strict=False)
        .dt.replace_time_zone("UTC")
        .alias("ts_utc")
    ).drop("ts_event")

    return df.select([
        "ts_utc", "instrument_id", "symbol",
        "open", "high", "low", "close", "volume",
    ])


def _join_pair(def_csv: Path, bars_csv: Path) -> pl.DataFrame:
    """Join one (definition, ohlcv-1m) pair into a per-leg time-series."""
    defs = _parse_definitions(def_csv)
    bars = _parse_ohlcv(bars_csv)

    joined = bars.join(defs, on="instrument_id", how="left")

    # Rows that didn't match a C/P definition row are multi-leg
    # user-defined-strategy combos (instrument_class='T' in the raw
    # definition file — verticals, butterflies, condors, etc., that
    # trade as one instrument).  Their symbol pattern looks like
    # 'UD:EN: VT 2523136'.  We exclude them because backtests build
    # combo positions from outright legs and never enter as a single
    # exchange-listed combo.  The count is 15-20% of bars in a typical
    # MES options dataset (large but expected for a healthy options
    # market that supports listed multi-leg execution).
    n_before = joined.height
    joined = joined.filter(pl.col("expiry").is_not_null())
    n_combo_bars_filtered = n_before - joined.height
    if n_combo_bars_filtered > 0:
        # Surface in the receipt — visible at audit but expected.
        joined = joined.with_columns(
            pl.lit(n_combo_bars_filtered).alias("__combo_bars_filtered")
        )

    return joined


def ingest_databento_options_pair(
    def_csv: Path,
    bars_csv: Path,
    source: DataSource,
    *,
    out_dir: Path | None = None,
    append: bool = False,
) -> IngestResult:
    """Ingest a single (definition, ohlcv-1m) CSV pair.

    The source's `IngestSpec.format` must be `databento_options_csv`.
    Output rows are sorted by (ts_utc, expiry, strike, side) and
    written as date-partitioned parquet.

    `append=True` means existing date partitions for the source are
    preserved; new rows are unioned in (and de-duped by ts_utc +
    instrument_id).  `append=False` (default) overwrites — appropriate
    for the first parent's pull; subsequent parents in a batch should
    pass `append=True` so the output for one source contains all
    parents' bars.
    """
    if not def_csv.exists():
        raise FileNotFoundError(def_csv)
    if not bars_csv.exists():
        raise FileNotFoundError(bars_csv)
    if source.ingest is None or source.ingest.format != "databento_options_csv":
        raise ValueError(
            f"source {source.id!r} is not declared as databento_options_csv "
            "(ingest.format mismatch)"
        )

    out_root = source_root(source.id, out_dir=out_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    joined = _join_pair(def_csv, bars_csv)
    rows_in_pair = joined.height
    if rows_in_pair == 0:
        raise ValueError(
            f"join of {def_csv.name} + {bars_csv.name} produced zero rows"
        )

    combo_bars_filtered = 0
    if "__combo_bars_filtered" in joined.columns:
        combo_bars_filtered = int(joined["__combo_bars_filtered"].max() or 0)
        joined = joined.drop("__combo_bars_filtered")

    if append:
        # Read existing partitions if any, union, dedupe.
        existing_pattern = str(out_root / "date=*" / "data.parquet")
        try:
            existing = pl.read_parquet(existing_pattern)
        except (FileNotFoundError, pl.exceptions.ComputeError):
            existing = None
        if existing is not None and existing.height > 0:
            unioned = pl.concat([existing, joined], how="vertical_relaxed")
            unioned = unioned.unique(
                subset=["ts_utc", "instrument_id"],
                keep="first",
                maintain_order=True,
            )
            joined = unioned

    joined = joined.sort(["ts_utc", "expiry", "strike", "side"])
    rows_out = joined.height
    coverage_start = joined["ts_utc"].min()
    coverage_end = joined["ts_utc"].max()

    partitions_written = write_date_partitions(joined, out_root)

    batch_id = compute_batch_id(bars_csv, source.id, source.version)
    write_receipt(
        out_root,
        batch_id,
        {
            "source_id": source.id,
            "source_version": source.version,
            "batch_id": batch_id,
            "format": "databento_options_csv",
            "definition_csv": str(def_csv),
            "definition_csv_sha256": sha256_file(def_csv),
            "ohlcv_csv": str(bars_csv),
            "ohlcv_csv_sha256": sha256_file(bars_csv),
            "ingested_at": now_utc_iso(),
            "rows_in_pair": rows_in_pair,
            "rows_out": rows_out,
            "combo_bars_filtered": combo_bars_filtered,
            "coverage_start": coverage_start.isoformat() if coverage_start else None,
            "coverage_end": coverage_end.isoformat() if coverage_end else None,
            "partitions": [str(p) for p in partitions_written],
            "append_mode": append,
        },
    )

    return IngestResult(
        source_id=source.id,
        batch_id=batch_id,
        raw_path=out_root,
        rows_in=rows_in_pair,
        rows_out=rows_out,
        duplicates_dropped=combo_bars_filtered,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        partitions_written=partitions_written,
    )


def ingest_databento_options_grid(
    pairs: list[tuple[Path, Path]],
    source: DataSource,
    *,
    out_dir: Path | None = None,
) -> IngestResult:
    """Ingest a batch of (definition, ohlcv-1m) CSV pairs into a
    single source, unioning all parents' rows together.

    The first pair is ingested with `append=False` (overwriting), then
    subsequent pairs are ingested with `append=True` to accumulate.

    Returns the IngestResult of the LAST pair (which by then contains
    the cumulative coverage and row count for the source).
    """
    if not pairs:
        raise ValueError("pairs list is empty")

    last_result: IngestResult | None = None
    for i, (def_csv, bars_csv) in enumerate(pairs):
        last_result = ingest_databento_options_pair(
            def_csv, bars_csv, source,
            out_dir=out_dir,
            append=(i > 0),
        )
    assert last_result is not None
    return last_result
