"""Shared helpers for raw-data ingest paths.

Multiple ingest formats (generic ts/price/size CSVs, Sierra Chart OHLCV CSVs,
future live-feed sinks) write into the same on-disk layout under
``data/raw/source=<id>/date=YYYY-MM-DD/data.parquet`` with matching receipts
under ``data/raw/source=<id>/_receipts/<batch_id>.json``.

Per 02_feature_pipeline.md, ingest must be deterministic and produce a
batch_id derived from the source identity and the input file content so the
same CSV always lands at the same logical batch.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from tradegy import config


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


def source_root(source_id: str, *, out_dir: Path | None = None) -> Path:
    """Resolve the on-disk root for a source's raw partitions."""
    return (out_dir or config.raw_dir()) / f"source={source_id}"


def write_date_partitions(
    df: pl.DataFrame, out_root: Path, *, date_column: str = "ts_utc"
) -> list[Path]:
    """Write a DataFrame as date-partitioned Parquet under ``out_root``.

    Adds a transient ``_date`` column derived from ``date_column``, groups by
    it, and writes each partition as ``date=YYYY-MM-DD/data.parquet`` with
    zstd compression. Returns the absolute paths written, in date order.
    """
    if df.height == 0:
        return []
    out_root.mkdir(parents=True, exist_ok=True)

    grouped = df.with_columns(pl.col(date_column).dt.date().alias("_date"))
    partitions_written: list[Path] = []
    for date_val, partition_df in grouped.group_by("_date", maintain_order=True):
        d = date_val[0]
        part_dir = out_root / f"date={d.isoformat()}"
        part_dir.mkdir(parents=True, exist_ok=True)
        out_path = part_dir / "data.parquet"
        partition_df.drop("_date").write_parquet(out_path, compression="zstd")
        partitions_written.append(out_path)
    return partitions_written


def write_receipt(
    out_root: Path,
    batch_id: str,
    receipt: dict[str, Any],
) -> Path:
    """Persist a batch receipt under ``<out_root>/_receipts/<batch_id>.json``."""
    receipts_dir = out_root / "_receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    path = receipts_dir / f"{batch_id}.json"
    path.write_text(json.dumps(receipt, indent=2))
    return path


def now_utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def read_raw(source_id: str, *, root: Path | None = None) -> pl.DataFrame:
    """Read all partitions for a source as a single sorted DataFrame.

    Format-agnostic — works for any ingest path that uses
    ``write_date_partitions``.
    """
    base = source_root(source_id, out_dir=root)
    if not base.exists():
        raise FileNotFoundError(f"no ingested data for source={source_id}")
    pattern = str(base / "date=*" / "data.parquet")
    df = pl.read_parquet(pattern)
    return df.sort("ts_utc")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_batch_id(
    csv_path: Path, source_id: str, source_version: str
) -> str:
    """Deterministic batch id: hash(source_id, source_version, file_sha256)."""
    h = hashlib.sha256()
    h.update(source_id.encode())
    h.update(b"\0")
    h.update(source_version.encode())
    h.update(b"\0")
    h.update(sha256_file(csv_path).encode())
    return h.hexdigest()[:16]
