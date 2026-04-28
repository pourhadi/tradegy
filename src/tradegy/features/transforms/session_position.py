"""session_position — fraction of the way through the current session.

For each bar's ts_utc, find the session it belongs to using the named
exchange calendar, then output:
    value = (ts_utc - session_open) / (session_close - session_open)
clamped to [0, 1]. 0.0 at the open, 1.0 at the close. Bars that fall
outside any session (e.g., between Mon close and next session's open
on a non-CMES calendar) are dropped.

Used as a time-of-day signal: opening drive, lunch lull, close behavior
all hang off this. Calendar-aware so DST and holidays are handled
correctly.

Input slot: bars (only ts_utc is consumed; other columns ignored).
Parameters:
    session_calendar: str (default "CMES") — exchange_calendars name.
Output: ts_utc, value (float in [0, 1]).
"""
from __future__ import annotations

from datetime import timedelta, timezone
from typing import Any

import exchange_calendars as xc
import polars as pl

from tradegy.features.transforms import register_transform


@register_transform("session_position")
def session_position(
    inputs: dict[str, pl.DataFrame], params: dict[str, Any]
) -> pl.DataFrame:
    if "bars" not in inputs:
        raise KeyError("session_position requires input named 'bars'")
    bars = inputs["bars"].sort("ts_utc")
    if "ts_utc" not in bars.columns:
        raise ValueError("session_position: input missing ts_utc")
    if bars.height == 0:
        return bars.select(pl.col("ts_utc"), pl.lit(None, dtype=pl.Float64).alias("value"))

    cal_name = params.get("session_calendar", "CMES")
    cal = xc.get_calendar(cal_name)

    min_ts = bars.select(pl.col("ts_utc").min()).item()
    max_ts = bars.select(pl.col("ts_utc").max()).item()
    # Pad both ends so a bar near a date boundary still finds its session.
    # CMES sessions are named by their close date but open 22:00 UTC the
    # *prior* day — so a bar at 23:00 UTC on date D belongs to session
    # D+1 and we'd miss it without the +1d pad on the upper end. The -1d
    # pad on the lower end is symmetric for any calendar with similar
    # quirks. The audit's no-lookahead check truncates inputs and would
    # otherwise drop these boundary rows on recompute.
    sessions = cal.sessions_in_range(
        (min_ts.date() - timedelta(days=1)).isoformat(),
        (max_ts.date() + timedelta(days=1)).isoformat(),
    )

    # Build a session lookup: (open_utc, close_utc) sorted by open.
    intervals: list[tuple[int, int]] = []  # nanosecond UTC bounds
    for sess in sessions:
        sess_open = cal.session_open(sess).to_pydatetime().astimezone(timezone.utc)
        sess_close = cal.session_close(sess).to_pydatetime().astimezone(timezone.utc)
        if sess_close > sess_open:
            # Convert to nanoseconds since epoch for fast Polars comparison.
            intervals.append(
                (int(sess_open.timestamp() * 1_000_000_000),
                 int(sess_close.timestamp() * 1_000_000_000))
            )
    if not intervals:
        return bars.select(
            pl.col("ts_utc"),
            pl.lit(None, dtype=pl.Float64).alias("value"),
        ).drop_nulls("value")

    sess_starts = pl.Series("sess_start", [iv[0] for iv in intervals], dtype=pl.Int64)
    sess_ends = pl.Series("sess_end", [iv[1] for iv in intervals], dtype=pl.Int64)
    sessions_df = pl.DataFrame({"sess_start": sess_starts, "sess_end": sess_ends})

    # For each bar timestamp, find the session whose [open, close)
    # contains it. join_asof on sess_start, then verify ts < sess_end.
    out = (
        bars.select(
            pl.col("ts_utc"),
            pl.col("ts_utc").dt.epoch(time_unit="ns").alias("ts_ns"),
        )
        .sort("ts_ns")
        .join_asof(
            sessions_df.sort("sess_start"),
            left_on="ts_ns",
            right_on="sess_start",
            strategy="backward",
        )
        .filter(pl.col("sess_start").is_not_null() & (pl.col("ts_ns") < pl.col("sess_end")))
        .with_columns(
            (
                (pl.col("ts_ns") - pl.col("sess_start"))
                / (pl.col("sess_end") - pl.col("sess_start"))
            ).cast(pl.Float64).alias("value")
        )
        .select(pl.col("ts_utc"), pl.col("value"))
        .sort("ts_utc")
    )
    return out
