"""time_since_last_event — hours since the last event of a filtered type.

Mirror of time_to_next_event but backward-asof. For each timeline
timestamp, output hours-since-now since the most recent prior event
of any matching event_type. Null for timestamps before the first
matching event.

Used by post-event strategies (post-FOMC reversion, post-CPI fade,
etc.) that need to know how recently the event fired.

Two inputs:
  * `events` — frame with `ts_utc` + `event_type`.
  * `timeline` — frame with `ts_utc` (the bars timeline).

Parameters:
    event_type_filter: list[str] (default ["fomc_statement"]).
    max_lookback_hours: float (default 168.0 = 1 week) — null for
        timeline timestamps further than this from the prior event.
"""
from __future__ import annotations

from typing import Any

import polars as pl

from tradegy.features.transforms import register_transform


@register_transform("time_since_last_event")
def time_since_last_event(
    inputs: dict[str, pl.DataFrame], parameters: dict[str, Any]
) -> pl.DataFrame:
    if "events" not in inputs:
        raise KeyError("time_since_last_event requires input named 'events'")
    if "timeline" not in inputs:
        raise KeyError("time_since_last_event requires input named 'timeline'")

    event_type_filter = parameters.get(
        "event_type_filter", ["fomc_statement"],
    )
    if not isinstance(event_type_filter, list) or not event_type_filter:
        raise ValueError(
            "event_type_filter must be a non-empty list of event_type strings"
        )
    max_lookback_hours = float(parameters.get("max_lookback_hours", 168.0))
    if max_lookback_hours <= 0:
        raise ValueError("max_lookback_hours must be > 0")

    events = inputs["events"]
    if "ts_utc" not in events.columns:
        raise ValueError("events frame missing 'ts_utc'")
    if "event_type" not in events.columns:
        raise ValueError("events frame missing 'event_type'")

    timeline = inputs["timeline"]
    if "ts_utc" not in timeline.columns:
        raise ValueError("timeline frame missing 'ts_utc'")

    relevant = (
        events
        .filter(pl.col("event_type").is_in(event_type_filter))
        .select("ts_utc")
        .unique(subset=["ts_utc"])
        .sort("ts_utc")
        .with_columns(pl.col("ts_utc").alias("event_ts"))
    )
    base = timeline.select("ts_utc").unique(subset=["ts_utc"]).sort("ts_utc")

    if relevant.height == 0:
        return base.head(0).with_columns(pl.lit(0.0).alias("value"))

    # Backward asof: for each timeline ts, the prior event_ts <= ts.
    joined = base.join_asof(
        relevant,
        on="ts_utc",
        right_on="event_ts",
        strategy="backward",
    )

    out = (
        joined
        .with_columns(
            ((pl.col("ts_utc") - pl.col("event_ts")).dt.total_seconds() / 3600.0)
            .alias("__hours_since")
        )
        .filter(pl.col("event_ts").is_not_null())
        .filter(pl.col("__hours_since") <= max_lookback_hours)
        .filter(pl.col("__hours_since") >= 0.0)
        .select(
            pl.col("ts_utc"),
            pl.col("__hours_since").cast(pl.Float64).alias("value"),
        )
        .sort("ts_utc")
    )
    return out
