"""prior_session_close — close price of the prior exchange session,
carried forward across the next session.

For a gap-fill strategy the canonical reference level is yesterday's
RTH close. This transform computes, for each bar inside an exchange-
calendar session, the close price of the bar at the close of the
*previous* session — published forward into the current session for
all bars from the current session_open to the current session_close.

Bars outside any session emit no value.

Implementation:
  1. Tag every input bar with its current session id (XNYS by default).
  2. For each session, take the close of the latest bar inside that
     session — that is `session_close_price[session_id]`.
  3. Publish each bar in session N with the
     session_close_price[session_(N-1)]. The earliest session has no
     prior; its bars emit no value.

Input slot: bars (ts_utc, close).
Parameters:
    session_calendar: str (default "XNYS").
Output: ts_utc, value.
"""
from __future__ import annotations

from datetime import timedelta, timezone
from typing import Any

import exchange_calendars as xc
import polars as pl

from tradegy.features.transforms import register_transform


@register_transform("prior_session_close")
def prior_session_close(
    inputs: dict[str, pl.DataFrame], parameters: dict[str, Any]
) -> pl.DataFrame:
    if "bars" not in inputs:
        raise KeyError("prior_session_close requires input named 'bars'")
    bars = inputs["bars"].sort("ts_utc")
    required = {"ts_utc", "close"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(
            f"prior_session_close: input missing columns {sorted(missing)}"
        )
    if bars.height == 0:
        return bars.select(
            pl.col("ts_utc"),
            pl.lit(None, dtype=pl.Float64).alias("value"),
        )

    cal_name = parameters.get("session_calendar", "XNYS")
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
        if sess_close <= sess_open:
            continue
        interval_rows.append({
            "__session_id": sess.date().isoformat(),
            "__sess_open": sess_open,
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

    # Per-session close = the close of the last bar inside that session.
    per_session_close = (
        tagged.sort(["__session_id", "ts_utc"])
        .group_by("__session_id", maintain_order=True)
        .agg(pl.col("close").last().alias("__session_close_price"))
        .sort("__session_id")
    )
    # Lag the per-session close forward by one session: session_id N
    # carries session_close_price[N-1].
    lagged = per_session_close.with_columns(
        pl.col("__session_close_price").shift(1).alias("__prior_close")
    ).select("__session_id", "__prior_close")

    out = (
        tagged.join(lagged, on="__session_id", how="inner")
        .filter(pl.col("__prior_close").is_not_null())
        .select(
            pl.col("ts_utc"),
            pl.col("__prior_close").alias("value"),
        )
        .sort("ts_utc")
    )
    return out
