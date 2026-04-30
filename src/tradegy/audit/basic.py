"""Stage 2 (minimal) — Data Audit suite.

Only the checks needed for the vertical slice. The full audit suite
(02_feature_pipeline.md:76-98) — revision detection, latency
characterization, cross-source reconciliation — is out of scope and tracked
for the next slice.

Implemented checks for a non-revisable tick or bar source:
  * row count + dedup count
  * monotonic-by-construction (ingestion sorts) sanity check
  * intra-row-gap distribution, with **session-aware exclusion** when the
    source declares a session_calendar — overnight maintenance halts,
    weekends, and holidays are not flagged.
  * value sanity (price/OHLC > 0, no NaN/Inf in declared float fields)
  * coverage start/end vs declared cadence

Findings are emitted with severity levels matching the spec
(02_feature_pipeline.md:88).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from tradegy import config
from tradegy.calendar import expected_non_session_intervals, is_expected_gap
from tradegy.ingest.csv_es import read_raw
from tradegy.types import AuditFinding, AuditReport, DataSource


# Field names that must be strictly positive (price-like).
_POSITIVE_FIELDS = ("price", "open", "high", "low", "close")
# Field names that must be non-negative (count- or quantity-like).
_NON_NEGATIVE_FIELDS = ("size", "volume", "num_trades", "bid_volume", "ask_volume")


def audit_source(
    source: DataSource,
    *,
    batch_id: str | None = None,
    max_gap_seconds: float | None = None,
    raw_root: Path | None = None,
    out_dir: Path | None = None,
) -> AuditReport:
    # Threshold precedence: explicit arg > source.max_inactivity_seconds > 60s default.
    if max_gap_seconds is None:
        max_gap_seconds = (
            source.max_inactivity_seconds
            if source.max_inactivity_seconds is not None
            else 60.0
        )

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
        gap_df = (
            df.select(
                pl.col("ts_utc"),
                pl.col("ts_utc").shift(1).alias("prev_ts"),
                (pl.col("ts_utc").diff().dt.total_microseconds() / 1_000_000).alias(
                    "gap_s"
                ),
            )
            .drop_nulls()
            .filter(pl.col("gap_s") > max_gap_seconds)
        )

        if gap_df.height > 0 and source.session_calendar is not None:
            # Subtract gaps that fall inside an expected non-session window.
            # gap_df columns are (ts_utc, prev_ts, gap_s) — the observed
            # gap runs from prev_ts (earlier) to ts_utc (later).
            min_ts: datetime = df.select(pl.col("ts_utc").min()).item()
            max_ts: datetime = df.select(pl.col("ts_utc").max()).item()
            expected = expected_non_session_intervals(
                source.session_calendar, min_ts, max_ts
            )
            keep_mask = []
            for ts, prev_ts, _gap in gap_df.iter_rows():
                # gap_start = prev_ts (earlier), gap_end = ts (later).
                if not is_expected_gap(prev_ts, ts, expected):
                    keep_mask.append(True)
                else:
                    keep_mask.append(False)
            gap_df = gap_df.filter(pl.Series(keep_mask))

        if gap_df.height > 0:
            max_gap = float(gap_df.get_column("gap_s").max())
            findings.append(
                AuditFinding(
                    severity="HIGH",
                    code="excessive_gap",
                    message=(
                        f"{gap_df.height} unexpected gap(s) > {max_gap_seconds}s; "
                        f"largest {max_gap:.2f}s"
                        + (" (after subtracting calendar non-sessions)"
                           if source.session_calendar else "")
                    ),
                    detail={
                        "max_gap_seconds": max_gap,
                        "gap_count": gap_df.height,
                        "session_calendar": source.session_calendar,
                    },
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

    for col_name in _POSITIVE_FIELDS:
        if col_name in df.columns:
            n_nonpos = int(
                df.select((pl.col(col_name) <= 0).sum()).item() or 0
            )
            if n_nonpos:
                findings.append(
                    AuditFinding(
                        severity="HIGH",
                        code="non_positive_value",
                        message=f"{n_nonpos} rows with non-positive {col_name}",
                        detail={"field": col_name, "count": n_nonpos},
                    )
                )

    for col_name in _NON_NEGATIVE_FIELDS:
        if col_name in df.columns:
            n_neg = int(
                df.select((pl.col(col_name) < 0).sum()).item() or 0
            )
            if n_neg:
                findings.append(
                    AuditFinding(
                        severity="HIGH",
                        code="negative_quantity",
                        message=f"{n_neg} rows with negative {col_name}",
                        detail={"field": col_name, "count": n_neg},
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
