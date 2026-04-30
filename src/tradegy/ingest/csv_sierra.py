"""Stage 1 — Sierra Chart OHLCV CSV ingest.

Sierra Chart exports tick/bar data with leading-whitespace headers and a
split-out Date + Time pair, single-digit month/day:

    Date, Time, Open, High, Low, Last, Volume, NumberOfTrades, BidVolume, AskVolume
    2019/5/6, 13:30:00, 2898.00, 2898.50, ...

Each row is a pre-aggregated bar (1-second for the ES file, 5-second for the
MES file). The bars carry signed-flow proxies (BidVolume / AskVolume) and a
trade count. We preserve all of it in the raw partition layout so downstream
transforms can opt in.

Per CLAUDE.md the 4.8 GB file MUST NOT be read eagerly. We use the lazy
Polars frame and `collect(engine="streaming")` over a per-year date-grouped
pipeline so peak memory stays bounded.

Per 02_feature_pipeline.md:73 the timezone of the input is declared by the
caller; DST ambiguity is raised, never silently resolved.
"""
from __future__ import annotations

from datetime import datetime, timezone
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


# Canonical OHLCV bar column names downstream transforms expect.
SIERRA_RENAME: dict[str, str] = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Last": "close",
    "Volume": "volume",
    "NumberOfTrades": "num_trades",
    "BidVolume": "bid_volume",
    "AskVolume": "ask_volume",
}


def ingest_sierra_csv(
    csv_path: Path,
    source: DataSource,
    *,
    input_tz: str,
    out_dir: Path | None = None,
    chunk_years: int = 1,
) -> IngestResult:
    """Ingest a Sierra Chart OHLCV CSV for a registered DataSource.

    Args:
        csv_path: path to the Sierra Chart CSV file.
        source: admitted DataSource registry entry whose `ingest` block
            declares ``format=sierra_chart_csv`` and the multi-column
            timestamp pair (typically ``[Date, Time]``).
        input_tz: IANA timezone of the timestamps in the CSV (e.g.,
            ``America/Chicago`` for CME exchange time). DST ambiguity is
            raised.
        out_dir: override for the data root.
        chunk_years: how many calendar years per streaming pass. Default 1
            keeps peak memory under ~1 GB for 1-second ES bars.
    """
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)
    if source.ingest is None or source.ingest.format != "sierra_chart_csv":
        raise ValueError(
            f"source {source.id!r} is not declared as sierra_chart_csv "
            "(ingest.format mismatch)"
        )
    ts_cols = source.ingest.timestamp_columns
    if not ts_cols or len(ts_cols) != 2:
        raise ValueError(
            f"source {source.id!r}: sierra_chart_csv expects exactly two "
            f"timestamp_columns [date, time]; got {ts_cols}"
        )
    date_col, time_col = ts_cols

    out_root = source_root(source.id, out_dir=out_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    # Lazy scan with all columns as Utf8 first; we strip whitespace from
    # headers immediately, then cast the numeric columns ourselves so the
    # parser never has to guess and never blows up on a stray space.
    lf = pl.scan_csv(csv_path, has_header=True, infer_schema_length=0)
    raw_columns = lf.collect_schema().names()
    stripped = {c: c.strip() for c in raw_columns}
    lf = lf.rename(stripped)

    expected = {date_col, time_col, *SIERRA_RENAME.keys()}
    actual = set(stripped.values())
    missing = expected - actual
    if missing:
        raise ValueError(
            f"Sierra CSV {csv_path.name} missing expected columns: "
            f"{sorted(missing)}; got {sorted(actual)}"
        )

    # Trim whitespace from EVERY string cell — Sierra prefixes a space to
    # every value after a comma (`, 2898.00`, `, 13:30:00`), and the numeric
    # casts below would all fail without this.
    lf = lf.with_columns(pl.col(pl.Utf8).str.strip_chars())

    # Parse Date as YYYY/M/D via int components — avoids the platform-
    # specific %-m strftime token. Time parses cleanly via str.to_time().
    date_parts = pl.col(date_col).str.split("/")
    year = date_parts.list.get(0).cast(pl.Int32, strict=True)
    month = date_parts.list.get(1).cast(pl.Int8, strict=True)
    day = date_parts.list.get(2).cast(pl.Int8, strict=True)
    time_parts = pl.col(time_col).str.split(":")
    hour = time_parts.list.get(0).cast(pl.Int8, strict=True)
    minute = time_parts.list.get(1).cast(pl.Int8, strict=True)
    second = time_parts.list.get(2).cast(pl.Int8, strict=True)

    lf = lf.with_columns(
        pl.datetime(
            year=year,
            month=month,
            day=day,
            hour=hour,
            minute=minute,
            second=second,
            time_unit="ns",
            time_zone=input_tz,
            ambiguous="raise",
        )
        .dt.convert_time_zone("UTC")
        .alias("ts_utc")
    )

    # Cast numeric columns and apply canonical rename in one pass.
    lf = lf.with_columns(
        [
            pl.col("Open").cast(pl.Float64, strict=True),
            pl.col("High").cast(pl.Float64, strict=True),
            pl.col("Low").cast(pl.Float64, strict=True),
            pl.col("Last").cast(pl.Float64, strict=True),
            pl.col("Volume").cast(pl.Int64, strict=True),
            pl.col("NumberOfTrades").cast(pl.Int64, strict=True),
            pl.col("BidVolume").cast(pl.Int64, strict=True),
            pl.col("AskVolume").cast(pl.Int64, strict=True),
        ]
    ).rename(SIERRA_RENAME)

    keep_cols = [
        "ts_utc",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "num_trades",
        "bid_volume",
        "ask_volume",
    ]
    lf = lf.select(keep_cols)

    # Determine the year span via a cheap scan so we can stream per chunk
    # without materializing the whole frame at once. A 4.8 GB CSV with
    # 250M rows would peak at ~30 GB if collected eagerly; per-year
    # streaming keeps peak under ~1 GB.
    year_bounds = (
        lf.select(
            pl.col("ts_utc").dt.year().min().alias("min_year"),
            pl.col("ts_utc").dt.year().max().alias("max_year"),
        )
        .collect(engine="streaming")
        .row(0, named=True)
    )
    min_year = int(year_bounds["min_year"])
    max_year = int(year_bounds["max_year"])

    rows_in = 0
    rows_out = 0
    duplicates_dropped = 0
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    partitions_written: list[Path] = []

    chunk_starts = list(range(min_year, max_year + 1, chunk_years))
    for start_year in chunk_starts:
        end_year = min(start_year + chunk_years - 1, max_year)
        chunk_lf = lf.filter(
            (pl.col("ts_utc").dt.year() >= start_year)
            & (pl.col("ts_utc").dt.year() <= end_year)
        )
        chunk_df = chunk_lf.collect(engine="streaming")
        if chunk_df.height == 0:
            continue
        chunk_in = chunk_df.height
        chunk_df = chunk_df.sort("ts_utc").unique(
            subset=["ts_utc"], keep="first", maintain_order=True
        )
        chunk_out = chunk_df.height
        rows_in += chunk_in
        rows_out += chunk_out
        duplicates_dropped += chunk_in - chunk_out

        chunk_min = chunk_df.select(pl.col("ts_utc").min()).item()
        chunk_max = chunk_df.select(pl.col("ts_utc").max()).item()
        coverage_start = chunk_min if coverage_start is None else min(coverage_start, chunk_min)
        coverage_end = chunk_max if coverage_end is None else max(coverage_end, chunk_max)

        partitions_written.extend(write_date_partitions(chunk_df, out_root))

    if rows_out == 0:
        raise ValueError(f"no rows after dedup for {csv_path}")
    assert coverage_start is not None and coverage_end is not None

    batch_id = compute_batch_id(csv_path, source.id, source.version)
    write_receipt(
        out_root,
        batch_id,
        {
            "source_id": source.id,
            "source_version": source.version,
            "batch_id": batch_id,
            "format": "sierra_chart_csv",
            "csv_path": str(csv_path),
            "csv_sha256": sha256_file(csv_path),
            "input_tz": input_tz,
            "ingested_at": now_utc_iso(),
            "rows_in": rows_in,
            "rows_out": rows_out,
            "duplicates_dropped": duplicates_dropped,
            "coverage_start": coverage_start.isoformat(),
            "coverage_end": coverage_end.isoformat(),
            "partitions": [str(p) for p in partitions_written],
            "column_remap": SIERRA_RENAME,
            "streaming_chunk_years": chunk_years,
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
