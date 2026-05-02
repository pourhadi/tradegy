"""opening_range_levels — RTH session opening-range high or low, carried
forward through the rest of the session.

For an Opening-Range Breakout (ORB) family strategy, the canonical
intraday reference levels are the high and low of the first N minutes
after the regular-session open (default 30 min, the "OR30"). After the
OR window closes, those two levels are static for the remainder of the
session and define the breakout / fade trigger zone.

This transform emits one value (high or low, controlled by the `level`
parameter) for every bar that falls AFTER the OR window inside the same
RTH session. Bars inside the OR window itself, and bars outside the
RTH session, get no output — the strategy can only act on a fully-
formed OR.

Sessions are derived from a configurable exchange_calendars calendar
(default `XNYS`, which is 09:30–16:00 ET — 15 minutes shorter than
ES/MES RTH at the close, but the OR depends only on the open and
matches exactly there). Override via the `session_calendar` parameter.

Input slot: `bars` (ts_utc, high, low).
Parameters:
    level: "high" | "low" (required).
    or_window_minutes: int (default 30).
    session_calendar: str (default "XNYS").
Output: ts_utc, value.
"""
from __future__ import annotations

from datetime import timedelta, timezone
from typing import Any

import exchange_calendars as xc
import polars as pl

from tradegy.features.transforms import register_transform


@register_transform("opening_range_levels")
def opening_range_levels(
    inputs: dict[str, pl.DataFrame], parameters: dict[str, Any]
) -> pl.DataFrame:
    if "bars" not in inputs:
        raise KeyError("opening_range_levels requires input named 'bars'")
    level = parameters.get("level")
    if level not in ("high", "low"):
        raise ValueError(
            f"opening_range_levels.level must be 'high' or 'low' (got {level!r})"
        )
    or_minutes = int(parameters.get("or_window_minutes", 30))
    if or_minutes <= 0:
        raise ValueError("or_window_minutes must be > 0")
    cal_name = parameters.get("session_calendar", "XNYS")

    bars = inputs["bars"].sort("ts_utc")
    required = {"ts_utc", "high", "low"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(
            f"opening_range_levels: input missing columns {sorted(missing)}"
        )
    if bars.height == 0:
        return bars.select(
            pl.col("ts_utc"),
            pl.lit(None, dtype=pl.Float64).alias("value"),
        )

    cal = xc.get_calendar(cal_name)
    min_ts = bars.select(pl.col("ts_utc").min()).item()
    max_ts = bars.select(pl.col("ts_utc").max()).item()
    sessions = cal.sessions_in_range(
        (min_ts.date() - timedelta(days=1)).isoformat(),
        (max_ts.date() + timedelta(days=1)).isoformat(),
    )
    interval_rows = []
    for sess in sessions:
        sess_open = cal.session_open(sess).to_pydatetime().astimezone(timezone.utc)
        sess_close = cal.session_close(sess).to_pydatetime().astimezone(timezone.utc)
        or_end = sess_open + timedelta(minutes=or_minutes)
        if sess_close <= or_end:
            continue
        interval_rows.append({
            "__session_id": sess.date().isoformat(),
            "__sess_open": sess_open,
            "__or_end": or_end,
            "__sess_close": sess_close,
        })
    if not interval_rows:
        return bars.select(
            pl.col("ts_utc"),
            pl.lit(None, dtype=pl.Float64).alias("value"),
        ).drop_nulls("value")

    sess_df = pl.DataFrame(
        interval_rows,
        schema={
            "__session_id": pl.Utf8,
            "__sess_open": pl.Datetime("ns", "UTC"),
            "__or_end": pl.Datetime("ns", "UTC"),
            "__sess_close": pl.Datetime("ns", "UTC"),
        },
    ).sort("__sess_open")

    tagged = (
        bars.with_columns(pl.col("ts_utc").cast(pl.Datetime("ns", "UTC")))
        .sort("ts_utc")
        .join_asof(
            sess_df,
            left_on="ts_utc",
            right_on="__sess_open",
            strategy="backward",
        )
        .filter(
            pl.col("__session_id").is_not_null()
            & (pl.col("ts_utc") < pl.col("__sess_close"))
        )
    )
    if tagged.height == 0:
        return bars.select(
            pl.col("ts_utc"),
            pl.lit(None, dtype=pl.Float64).alias("value"),
        ).drop_nulls("value")

    # Aggregate the OR window's extremes per session.
    in_or_window = pl.col("ts_utc") < pl.col("__or_end")
    if level == "high":
        or_value_expr = pl.when(in_or_window).then(pl.col("high")).otherwise(None)
        agg_expr = or_value_expr.max().over("__session_id").alias("__or_value")
    else:  # low
        or_value_expr = pl.when(in_or_window).then(pl.col("low")).otherwise(None)
        agg_expr = or_value_expr.min().over("__session_id").alias("__or_value")

    out = (
        tagged.with_columns(agg_expr)
        .filter(
            (pl.col("ts_utc") >= pl.col("__or_end"))
            & pl.col("__or_value").is_not_null()
        )
        .select(
            pl.col("ts_utc"),
            pl.col("__or_value").alias("value"),
        )
        .sort("ts_utc")
    )
    return out
