"""Stage 1 — Data Ingestion for ES tick CSVs.

Reads a CSV of ES tick data, normalizes timestamps to UTC nanosecond
precision, deduplicates exact-duplicate rows, and writes append-only,
date-partitioned Parquet under data/raw/source=<id>/date=YYYY-MM-DD/.

The source's `timestamp_column` and the timezone of incoming timestamps are
declared by the caller (via the data-source registry entry, not inferred
later) per 02_feature_pipeline.md:60-66.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from tradegy import config
from tradegy.types import DataSource


@dataclass
class IngestResult:
    source_id: str
    batch_id: str
    raw_path: Path
    rows_in: int
    rows_out: int
    duplicates_dropped: int
    coverage_start: datetime
    coverage_end: datetime
    partitions_written: list[Path]


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
    """Ingest a CSV file for a registered data source.

    Args:
        csv_path: path to the CSV file.
        source: the admitted DataSource registry entry.
        input_tz: IANA timezone of the timestamp column in the CSV. Required
            because timezone ambiguity in DST transitions is a flagged failure
            mode (02_feature_pipeline.md:73). If the CSV is already UTC, pass
            "UTC".
        out_dir: override for output root (defaults to data/raw).
    """
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)

    out_root = (out_dir or config.raw_dir()) / f"source={source.id}"
    out_root.mkdir(parents=True, exist_ok=True)

    schema = _csv_schema_for(source)
    df = pl.read_csv(csv_path, schema_overrides=schema)
    rows_in = df.height

    ts_col = source.timestamp_column
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

    coverage_start: datetime = df.select(pl.col("ts_utc").min()).item()
    coverage_end: datetime = df.select(pl.col("ts_utc").max()).item()

    df = df.with_columns(pl.col("ts_utc").dt.date().alias("_date"))

    partitions_written: list[Path] = []
    for date_val, partition_df in df.group_by("_date", maintain_order=True):
        d = date_val[0]
        part_dir = out_root / f"date={d.isoformat()}"
        part_dir.mkdir(parents=True, exist_ok=True)
        out_path = part_dir / "data.parquet"
        partition_df.drop("_date").write_parquet(out_path, compression="zstd")
        partitions_written.append(out_path)

    batch_id = _compute_batch_id(csv_path, source.id, source.version)
    receipt = {
        "source_id": source.id,
        "source_version": source.version,
        "batch_id": batch_id,
        "csv_path": str(csv_path),
        "csv_sha256": _sha256_file(csv_path),
        "input_tz": input_tz,
        "ingested_at": datetime.now(tz=timezone.utc).isoformat(),
        "rows_in": rows_in,
        "rows_out": rows_out,
        "duplicates_dropped": duplicates_dropped,
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "partitions": [str(p) for p in partitions_written],
    }
    receipts_dir = out_root / "_receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    (receipts_dir / f"{batch_id}.json").write_text(json.dumps(receipt, indent=2))

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


def read_raw(source_id: str, *, root: Path | None = None) -> pl.DataFrame:
    """Read all partitions for a source as a single sorted DataFrame."""
    base = (root or config.raw_dir()) / f"source={source_id}"
    if not base.exists():
        raise FileNotFoundError(f"no ingested data for source={source_id}")
    pattern = str(base / "date=*" / "data.parquet")
    df = pl.read_parquet(pattern)
    return df.sort("ts_utc")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _compute_batch_id(csv_path: Path, source_id: str, source_version: str) -> str:
    h = hashlib.sha256()
    h.update(source_id.encode())
    h.update(b"\0")
    h.update(source_version.encode())
    h.update(b"\0")
    h.update(_sha256_file(csv_path).encode())
    return h.hexdigest()[:16]
