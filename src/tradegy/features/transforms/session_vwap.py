"""session_vwap — typical-price VWAP that resets at each exchange session open.

Volume-Weighted Average Price within a session is one of the most
common futures intraday reference levels. This transform produces:

    typical_price_t = (high + low + close) / 3
    cum_pv_t = sum(typical_price_i * volume_i for i in [session_open .. t])
    cum_v_t  = sum(volume_i             for i in [session_open .. t])
    vwap_t   = cum_pv_t / cum_v_t

with cumulative sums reset at each session boundary defined by the
named exchange_calendars calendar.

Bars that fall outside any session (deep weekend / holiday) get null
session_id and are dropped — they emit no VWAP value. Within-session
bars before any volume traded (rare, only at the very first bar) get
null too and are dropped.

Input slot: bars (ts_utc, high, low, close, volume).
Parameters:
    session_calendar: str (default "CMES").
Output: ts_utc, value (the VWAP at that bar).
"""
from __future__ import annotations

from datetime import timedelta, timezone
from typing import Any

import exchange_calendars as xc
import polars as pl

from tradegy.features.transforms import register_transform


@register_transform("session_vwap")
def session_vwap(
    inputs: dict[str, pl.DataFrame], parameters: dict[str, Any]
) -> pl.DataFrame:
    if "bars" not in inputs:
        raise KeyError("session_vwap requires input named 'bars'")
    bars = inputs["bars"].sort("ts_utc")
    required = {"ts_utc", "high", "low", "close", "volume"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"session_vwap: input missing columns {sorted(missing)}")
    if bars.height == 0:
        return bars.select(
            pl.col("ts_utc"),
            pl.lit(None, dtype=pl.Float64).alias("value"),
        )

    cal_name = parameters.get("session_calendar", "CMES")
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

    typical = (pl.col("high") + pl.col("low") + pl.col("close")) / 3.0
    pv = typical * pl.col("volume")
    cum_pv = pv.cum_sum().over("__session_id")
    cum_v = pl.col("volume").cum_sum().over("__session_id")
    out = (
        tagged.with_columns(
            pl.when(cum_v > 0)
            .then(cum_pv / cum_v)
            .otherwise(None)
            .alias("value")
        )
        .select(pl.col("ts_utc"), pl.col("value"))
        .drop_nulls("value")
        .sort("ts_utc")
    )
    return out
