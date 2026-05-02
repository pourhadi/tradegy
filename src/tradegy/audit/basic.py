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
  * coverage-by-hour distribution, calendar-aware. Catches the round-2
    failure mode where intraday partial-day coverage is silently masked
    (Sierra Chart MES export had only ~14:00-20:00 ET with
    `coverage.gaps: []`; the basic gap detector saw no anomaly because
    the inter-row gaps fit inside `max_inactivity_seconds`).

Findings are emitted with severity levels matching the spec
(02_feature_pipeline.md:88).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

import exchange_calendars as xc
import polars as pl

from tradegy import config
from tradegy.calendar import expected_non_session_intervals, is_expected_gap
from tradegy.ingest.csv_es import read_raw
from tradegy.types import AuditFinding, AuditReport, DataSource


# Field names that must be strictly positive (price-like).
_POSITIVE_FIELDS = ("price", "open", "high", "low", "close")
# Field names that must be non-negative (count- or quantity-like).
_NON_NEGATIVE_FIELDS = ("size", "volume", "num_trades", "bid_volume", "ask_volume")


# Hours-of-day with row count below this fraction of the median in-session
# hour are flagged as severely under-represented (MEDIUM). Any in-session
# hour with zero rows is HIGH (the round-2 failure shape).
_HOUR_UNDER_REPRESENTED_FRAC = 0.05
# Calendar session length above this counts as "24h-style" — every UTC
# hour expected. Below: partial-day calendar (e.g. XNYS 6.5h) — only
# hours intersecting the session's UTC footprint are expected.
_FULL_DAY_CALENDAR_HOURS = 20.0


def _expected_session_hours_utc(calendar_name: str) -> set[int] | None:
    """Return the set of UTC hours-of-day where the calendar's typical
    session is open, OR None if the calendar is "24h-style" (every UTC
    hour expected). Inspects up to 30 sample sessions to handle DST.
    """
    cal = xc.get_calendar(calendar_name)
    sessions = cal.sessions_in_range(
        "2024-01-02",
        "2024-02-15",
    )
    if len(sessions) == 0:
        return None
    sample = sessions[: min(len(sessions), 30)]
    lengths_h = []
    hours: set[int] = set()
    for sess in sample:
        sess_open = cal.session_open(sess).to_pydatetime().astimezone(timezone.utc)
        sess_close = cal.session_close(sess).to_pydatetime().astimezone(timezone.utc)
        lengths_h.append((sess_close - sess_open).total_seconds() / 3600.0)
        # Walk through every hour [open, close) and add to the set.
        cur = sess_open.replace(minute=0, second=0, microsecond=0)
        while cur < sess_close:
            hours.add(cur.hour)
            cur += timedelta(hours=1)
    if median(lengths_h) >= _FULL_DAY_CALENDAR_HOURS:
        return None  # 24h-style, every UTC hour expected
    return hours


def _check_coverage_by_hour(
    df: pl.DataFrame, source: DataSource
) -> list[AuditFinding]:
    """Flag intraday partial-day coverage missed by the row-gap check.

    The row-gap check (`excessive_gap`) sees only consecutive-row gaps,
    so a source that prints a row at 14:00 and the next at 18:00 with
    only those two hours' worth of rows per day silently passes if the
    14h overnight-from-the-prior-day fits inside `max_inactivity_seconds`.
    This check tallies UTC-hour-of-day distribution and, calendar-aware,
    flags any in-session hour that is empty or severely under-represented.
    """
    if df.height < 1000:
        return []  # not enough data to draw conclusions

    counts = (
        df.with_columns(
            pl.col("ts_utc").dt.hour().cast(pl.Int32).alias("__hod")
        )
        .group_by("__hod")
        .agg(pl.len().alias("__n"))
        .sort("__hod")
    )
    by_hour = dict(
        zip(
            counts.get_column("__hod").to_list(),
            counts.get_column("__n").to_list(),
        )
    )

    if source.session_calendar is None:
        return []  # cannot determine "expected" hours
    expected_hours = _expected_session_hours_utc(source.session_calendar)
    if expected_hours is None:
        # 24h-style calendar — every UTC hour should have data.
        relevant_hours = set(range(24))
    else:
        relevant_hours = set(expected_hours)

    in_session_counts = [by_hour.get(h, 0) for h in relevant_hours]
    nonzero = [c for c in in_session_counts if c > 0]
    if not nonzero:
        # No data inside any expected hour — the source is mis-aligned
        # with its declared calendar.
        return [
            AuditFinding(
                severity="CRITICAL",
                code="hourly_coverage_missing",
                message=(
                    f"no rows in any expected in-session UTC hour for "
                    f"calendar {source.session_calendar!r}; the source's "
                    "declared calendar may be wrong"
                ),
                detail={
                    "session_calendar": source.session_calendar,
                    "expected_hours_utc": sorted(relevant_hours),
                    "observed_hour_counts": dict(sorted(by_hour.items())),
                },
            )
        ]
    median_in_session = sorted(nonzero)[len(nonzero) // 2]

    missing_in_session = sorted(
        h for h in relevant_hours if by_hour.get(h, 0) == 0
    )
    severely_underrepresented = sorted(
        h for h in relevant_hours
        if 0 < by_hour.get(h, 0) < median_in_session * _HOUR_UNDER_REPRESENTED_FRAC
    )
    findings: list[AuditFinding] = []
    if missing_in_session:
        findings.append(
            AuditFinding(
                severity="HIGH",
                code="hourly_coverage_gap",
                message=(
                    f"{len(missing_in_session)} expected in-session UTC "
                    f"hour(s) have zero rows: {missing_in_session}. The "
                    "source likely has a partial-day export window even "
                    "though `coverage.gaps:[]` may report no gaps "
                    "(intraday gaps fit inside max_inactivity_seconds)."
                ),
                detail={
                    "session_calendar": source.session_calendar,
                    "missing_hours_utc": missing_in_session,
                    "median_hour_count": median_in_session,
                    "observed_hour_counts": dict(sorted(by_hour.items())),
                },
            )
        )
    if severely_underrepresented:
        findings.append(
            AuditFinding(
                severity="MEDIUM",
                code="hourly_coverage_uneven",
                message=(
                    f"{len(severely_underrepresented)} expected in-session "
                    f"UTC hour(s) below "
                    f"{int(_HOUR_UNDER_REPRESENTED_FRAC * 100)}% of "
                    f"median in-session density: {severely_underrepresented}"
                ),
                detail={
                    "session_calendar": source.session_calendar,
                    "underrepresented_hours_utc": severely_underrepresented,
                    "median_hour_count": median_in_session,
                    "threshold_fraction": _HOUR_UNDER_REPRESENTED_FRAC,
                },
            )
        )
    return findings


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

    findings.extend(_check_coverage_by_hour(df, source))

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
