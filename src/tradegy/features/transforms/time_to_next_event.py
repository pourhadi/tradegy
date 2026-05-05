"""time_to_next_event — hours until the next event of a filtered type.

For each timeline timestamp, output the hours-from-now until the next
event in `events` whose `event_type` is in `event_type_filter`. If no
future event exists in the events frame after the timeline timestamp,
output is null and the row is dropped.

Two inputs:
  * `events` — a frame with `ts_utc` and `event_type` columns.
  * `timeline` — a frame with `ts_utc` (the bars timeline). Output
    cadence matches the timeline's.

Parameters:
    event_type_filter: list[str] (default ["fomc_statement"]) —
        only events with `event_type` in this list count.
    max_lookahead_hours: float (default 168.0 = 1 week) — emit null
        for timeline timestamps further than this from the next
        matching event. Filters out boundary edges.

Output: ts_utc, value (float, hours until next event).

Used by event-anchored strategies (pre-FOMC drift, NFP fade, etc.)
that need to know how far ahead the next scheduled event is to
trigger a window-based entry.
"""
from __future__ import annotations

from typing import Any

import polars as pl

from tradegy.features.transforms import register_transform


@register_transform("time_to_next_event")
def time_to_next_event(
    inputs: dict[str, pl.DataFrame], parameters: dict[str, Any]
) -> pl.DataFrame:
    if "events" not in inputs:
        raise KeyError("time_to_next_event requires input named 'events'")
    if "timeline" not in inputs:
        raise KeyError("time_to_next_event requires input named 'timeline'")

    event_type_filter = parameters.get(
        "event_type_filter", ["fomc_statement"],
    )
    if not isinstance(event_type_filter, list) or not event_type_filter:
        raise ValueError(
            "event_type_filter must be a non-empty list of event_type strings"
        )
    max_lookahead_hours = float(parameters.get("max_lookahead_hours", 168.0))
    if max_lookahead_hours <= 0:
        raise ValueError("max_lookahead_hours must be > 0")

    events = inputs["events"]
    if "ts_utc" not in events.columns:
        raise ValueError("events frame missing 'ts_utc'")
    if "event_type" not in events.columns:
        raise ValueError("events frame missing 'event_type'")

    timeline = inputs["timeline"]
    if "ts_utc" not in timeline.columns:
        raise ValueError("timeline frame missing 'ts_utc'")

    # Filter events to the configured types.
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
        # No relevant events → all-null output, dropped.
        return base.head(0).with_columns(pl.lit(0.0).alias("value"))

    # Forward asof: for each timeline ts, the next event_ts >= ts. If
    # no such event exists, ts is past the last event → drop.
    joined = base.join_asof(
        relevant,
        on="ts_utc",
        right_on="event_ts",
        strategy="forward",
    )

    out = (
        joined
        .with_columns(
            ((pl.col("event_ts") - pl.col("ts_utc")).dt.total_seconds() / 3600.0)
            .alias("__hours_until")
        )
        .filter(pl.col("event_ts").is_not_null())
        .filter(pl.col("__hours_until") <= max_lookahead_hours)
        .filter(pl.col("__hours_until") >= 0.0)
        .select(
            pl.col("ts_utc"),
            pl.col("__hours_until").cast(pl.Float64).alias("value"),
        )
        .sort("ts_utc")
    )
    return out
