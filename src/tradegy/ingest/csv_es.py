"""Stage 1 — generic ts/price/size CSV ingest.

Reads a CSV declared by a generic-format DataSource (single timestamp column,
declared columns), normalizes timestamps to UTC nanosecond precision,
deduplicates exact-duplicate rows, and writes append-only, date-partitioned
Parquet under ``data/raw/source=<id>/date=YYYY-MM-DD/``.

Per 02_feature_pipeline.md:60-66 the source's ``timestamp_column`` and the
input timezone are declared by the caller; timezone ambiguity in DST
transitions is a flagged failure mode and is raised here, not silently
resolved.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from tradegy.ingest._common import (
    IngestResult,
    compute_batch_id,
    now_utc_iso,
    read_raw,  # noqa: F401  — re-exported for back-compat
    sha256_file,
    source_root,
    write_date_partitions,
    write_receipt,
)
from tradegy.types import DataSource


def _csv_schema_for(source: DataSource) -> dict[str, pl.DataType]:
    type_map: dict[str, pl.DataType] = {
        "timestamp": pl.String,
        "datetime": pl.String,
        "string": pl.String,
        "float": pl.Float64,
        "int": pl.Int64,
    }
    return {f.name: type_map.get(f.type, pl.String) for f in source.fields}


def ingest_csv(
    csv_path: Path,
    source: DataSource,
    *,
    input_tz: str,
    out_dir: Path | None = None,
) -> IngestResult:
    """Ingest a generic ts/price/size CSV for a registered data source."""
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    out_root = source_root(source.id, out_dir=out_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    schema = _csv_schema_for(source)
    df = pl.read_csv(csv_path, schema_overrides=schema)
    rows_in = df.height

    ts_col = source.timestamp_column
    if ts_col is None:
        raise ValueError(
            f"source {source.id!r} declares no timestamp_column; "
            "generic_csv ingest requires one"
        )
    if ts_col not in df.columns:
        raise ValueError(
            f"timestamp column '{ts_col}' not in CSV columns {df.columns}"
        )

    df = df.with_columns(
        pl.col(ts_col)
        .str.to_datetime(time_unit="ns", time_zone=input_tz, strict=True)
        .dt.convert_time_zone("UTC")
        .alias("ts_utc")
    )

    df = df.sort("ts_utc").unique(maintain_order=True)
    rows_out = df.height
    duplicates_dropped = rows_in - rows_out

    if rows_out == 0:
        raise ValueError(f"no rows after dedup for {csv_path}")

    coverage_start = df.select(pl.col("ts_utc").min()).item()
    coverage_end = df.select(pl.col("ts_utc").max()).item()

    partitions_written = write_date_partitions(df, out_root)

    batch_id = compute_batch_id(csv_path, source.id, source.version)
    write_receipt(
        out_root,
        batch_id,
        {
            "source_id": source.id,
            "source_version": source.version,
            "batch_id": batch_id,
            "format": "generic_csv",
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
