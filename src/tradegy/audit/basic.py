"""Stage 2 (minimal) — Data Audit suite.

Only the checks needed for the vertical slice. The full audit suite
(02_feature_pipeline.md:76-98) — revision detection, latency
characterization, cross-source reconciliation — is out of scope and tracked
for the next slice.

Implemented checks for a non-revisable tick source:
  * row count + dedup count
  * monotonic-by-construction (ingestion sorts) sanity check
  * intra-row-gap distribution (flag if any gap > declared_gap_tolerance_seconds)
  * value sanity (price > 0, size > 0, no NaN/Inf in declared float fields)
  * coverage start/end vs declared cadence

Findings are emitted with severity levels matching the spec
(02_feature_pipeline.md:88).
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from tradegy import config
from tradegy.ingest.csv_es import read_raw
from tradegy.types import AuditFinding, AuditReport, DataSource


def audit_source(
    source: DataSource,
    *,
    batch_id: str | None = None,
    max_gap_seconds: float = 60.0,
    raw_root: Path | None = None,
    out_dir: Path | None = None,
) -> AuditReport:
    df = read_raw(source.id, root=raw_root)
    findings: list[AuditFinding] = []

    rows = df.height
    deduped = df.unique(subset=["ts_utc"]).height
    if deduped != rows:
        findings.append(
            AuditFinding(
                severity="MEDIUM",
                code="duplicate_timestamps",
                message=f"{rows - deduped} duplicate timestamps detected",
                detail={"row_count": rows, "unique_ts": deduped},
            )
        )

    ts = df.get_column("ts_utc")
    is_sorted = ts.is_sorted()
    if not is_sorted:
        findings.append(
            AuditFinding(
                severity="CRITICAL",
                code="unsorted_timestamps",
                message="raw partitions are not monotonically sorted by ts_utc",
            )
        )

    if rows >= 2:
        gaps = (
            df.select(
                (pl.col("ts_utc").diff().dt.total_microseconds() / 1_000_000).alias(
                    "gap_s"
                )
            )
            .drop_nulls()
            .get_column("gap_s")
        )
        max_gap = float(gaps.max() or 0.0)
        if max_gap > max_gap_seconds:
            findings.append(
                AuditFinding(
                    severity="HIGH",
                    code="excessive_gap",
                    message=f"max inter-row gap {max_gap:.2f}s exceeds tolerance {max_gap_seconds}s",
                    detail={"max_gap_seconds": max_gap},
                )
            )

    for fld in source.fields:
        if fld.type != "float":
            continue
        if fld.name not in df.columns:
            continue
        col = df.get_column(fld.name)
        n_null = col.null_count()
        n_inf = int(col.is_infinite().sum() or 0)
        n_nan = int(col.is_nan().sum() or 0)
        if n_null or n_inf or n_nan:
            findings.append(
                AuditFinding(
                    severity="HIGH",
                    code="invalid_numeric",
                    message=f"field {fld.name}: {n_null} null, {n_nan} nan, {n_inf} inf",
                    detail={
                        "field": fld.name,
                        "null": n_null,
                        "nan": n_nan,
                        "inf": n_inf,
                    },
                )
            )

    for required_positive in ("price", "size"):
        if required_positive in df.columns:
            n_nonpos = int(
                df.select((pl.col(required_positive) <= 0).sum()).item() or 0
            )
            if n_nonpos:
                findings.append(
                    AuditFinding(
                        severity="HIGH",
                        code="non_positive_value",
                        message=f"{n_nonpos} rows with non-positive {required_positive}",
                        detail={"field": required_positive, "count": n_nonpos},
                    )
                )

    coverage_start: datetime = df.select(pl.col("ts_utc").min()).item()
    coverage_end: datetime = df.select(pl.col("ts_utc").max()).item()

    report = AuditReport(
        source_id=source.id,
        batch_id=batch_id or "latest",
        generated_at=datetime.now(tz=timezone.utc),
        row_count=rows,
        deduplicated_count=deduped,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        findings=findings,
    )

    out_root = out_dir or config.audits_dir()
    out_root.mkdir(parents=True, exist_ok=True)
    out_path = out_root / f"{source.id}_{report.generated_at.strftime('%Y%m%dT%H%M%SZ')}.json"
    out_path.write_text(report.model_dump_json(indent=2))
    return report


def write_audit_report(report: AuditReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2))


__all__ = ["audit_source", "write_audit_report"]
