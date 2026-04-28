"""Session-aware excessive_gap suppression in audit/basic.py.

Without a session calendar, the naive audit fires HIGH `excessive_gap` on
every overnight close, weekend, and holiday — for a 9-year continuous CME
dataset that's hundreds of false alarms. With session_calendar declared on
the source, only gaps that fall *outside* expected non-session intervals
are flagged.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl

from tradegy.audit.basic import audit_source
from tradegy.calendar import expected_non_session_intervals, is_expected_gap
from tradegy.ingest._common import write_date_partitions
from tradegy.types import (
    AvailabilityLatency,
    Coverage,
    DataSource,
    FieldSpec,
)


def _bar_source(session_calendar: str | None) -> DataSource:
    return DataSource(
        id="cal_test",
        version="v1",
        description="t",
        type="market_data",
        provider="local",
        revisable=False,
        revision_policy="never_revised",
        admission_rationale="t",
        coverage=Coverage(start_date=date(2024, 6, 3), end_date=date(2024, 6, 5)),
        cadence="1m",
        fields=[
            FieldSpec(name="ts_utc", type="timestamp"),
            FieldSpec(name="open", type="float"),
            FieldSpec(name="high", type="float"),
            FieldSpec(name="low", type="float"),
            FieldSpec(name="close", type="float"),
            FieldSpec(name="volume", type="int"),
        ],
        timestamp_column="ts_utc",
        availability_latency=AvailabilityLatency(median_seconds=0.0, p99_seconds=0.0),
        session_calendar=session_calendar,
    )


def _two_session_bars() -> pl.DataFrame:
    """Three bars in one session, then a 22h+ gap to the next session.

    Session N-1 ends 21:00 UTC (16:00 CT). Session N opens 22:00 UTC
    (17:00 CT). The gap from 21:00 → 22:00 is the daily maintenance halt
    and is the kind of expected non-session interval the calendar should
    suppress.
    """
    rows = [
        # Three minute-bars at the very end of session 2024-06-04 (close 21:00 UTC).
        (datetime(2024, 6, 4, 20, 58, tzinfo=timezone.utc), 5300.0, 5301.0, 5299.5, 5300.5, 100),
        (datetime(2024, 6, 4, 20, 59, tzinfo=timezone.utc), 5300.5, 5301.5, 5300.0, 5301.0, 110),
        (datetime(2024, 6, 4, 21, 0, tzinfo=timezone.utc), 5301.0, 5302.0, 5300.5, 5301.5, 120),
        # Resume at session 2024-06-05 open (22:00 UTC of 2024-06-04).
        (datetime(2024, 6, 4, 22, 0, tzinfo=timezone.utc), 5301.5, 5302.5, 5301.0, 5302.0, 130),
        (datetime(2024, 6, 4, 22, 1, tzinfo=timezone.utc), 5302.0, 5303.0, 5301.5, 5302.5, 140),
    ]
    return pl.DataFrame(
        {
            "ts_utc": [r[0] for r in rows],
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
            "volume": [r[5] for r in rows],
        },
        schema={
            "ts_utc": pl.Datetime("ns", "UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
    )


def test_expected_non_session_includes_daily_maintenance() -> None:
    intervals = expected_non_session_intervals(
        "CMES",
        datetime(2024, 6, 3, tzinfo=timezone.utc),
        datetime(2024, 6, 7, tzinfo=timezone.utc),
    )
    # Every 22h-close → 23h-open daily gap should be present (≥ 4 such gaps
    # across Mon-Thu close to Tue-Fri open, plus the weekend gap).
    assert len(intervals) >= 3
    # Each interval is well-formed.
    assert all(end > start for start, end in intervals)


def test_is_expected_gap_inside_interval() -> None:
    intervals = [
        (datetime(2024, 6, 4, 21, 0, tzinfo=timezone.utc),
         datetime(2024, 6, 4, 22, 0, tzinfo=timezone.utc)),
    ]
    assert is_expected_gap(
        datetime(2024, 6, 4, 21, 0, tzinfo=timezone.utc),
        datetime(2024, 6, 4, 22, 0, tzinfo=timezone.utc),
        intervals,
    )
    assert not is_expected_gap(
        datetime(2024, 6, 4, 14, 30, tzinfo=timezone.utc),
        datetime(2024, 6, 4, 16, 0, tzinfo=timezone.utc),
        intervals,
    )


def test_audit_without_calendar_flags_session_gap(tmp_path: Path) -> None:
    src = _bar_source(session_calendar=None)
    out_root = tmp_path / "raw" / f"source={src.id}"
    write_date_partitions(_two_session_bars(), out_root)

    report = audit_source(
        src,
        max_gap_seconds=60.0,
        raw_root=tmp_path / "raw",
        out_dir=tmp_path / "audits",
    )
    excessive = [f for f in report.findings if f.code == "excessive_gap"]
    assert excessive, "without calendar, the 1h maintenance gap should fire"


def test_audit_with_cmes_calendar_suppresses_session_gap(tmp_path: Path) -> None:
    src = _bar_source(session_calendar="CMES")
    out_root = tmp_path / "raw" / f"source={src.id}"
    write_date_partitions(_two_session_bars(), out_root)

    report = audit_source(
        src,
        max_gap_seconds=60.0,
        raw_root=tmp_path / "raw",
        out_dir=tmp_path / "audits",
    )
    excessive = [f for f in report.findings if f.code == "excessive_gap"]
    assert not excessive, (
        f"calendar should suppress maintenance gap; got: {excessive}"
    )
