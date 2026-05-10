"""event_reaction_return — signed post-event first-move return.

For each selected economic event, compute the return from the most recent
bar close at or before the event timestamp to the most recent bar close at
or before ``event_ts + reaction_minutes``. Broadcast that signed return to
timeline bars only after the reaction window has completed.

This is deliberately point-in-time: bars before ``reaction_end_ts`` receive
no value, so a strategy cannot know the first-move direction until the
reaction interval has actually elapsed.
"""
from __future__ import annotations

from typing import Any

import polars as pl

from tradegy.features.transforms import register_transform


@register_transform("event_reaction_return")
def event_reaction_return(
    inputs: dict[str, pl.DataFrame], parameters: dict[str, Any]
) -> pl.DataFrame:
    if "events" not in inputs:
        raise KeyError("event_reaction_return requires input named 'events'")
    if "timeline" not in inputs:
        raise KeyError("event_reaction_return requires input named 'timeline'")

    event_type_filter = parameters.get("event_type_filter", ["cpi"])
    if not isinstance(event_type_filter, list) or not event_type_filter:
        raise ValueError("event_type_filter must be a non-empty list")
    reaction_minutes = int(parameters.get("reaction_minutes", 30))
    if reaction_minutes <= 0:
        raise ValueError("reaction_minutes must be > 0")
    max_lookback_hours = float(parameters.get("max_lookback_hours", 6.0))
    if max_lookback_hours <= 0:
        raise ValueError("max_lookback_hours must be > 0")
    price_column = str(parameters.get("price_column", "close"))

    events = inputs["events"]
    if "ts_utc" not in events.columns:
        raise ValueError("events frame missing 'ts_utc'")
    if "event_type" not in events.columns:
        raise ValueError("events frame missing 'event_type'")

    timeline = inputs["timeline"]
    if "ts_utc" not in timeline.columns:
        raise ValueError("timeline frame missing 'ts_utc'")
    if price_column not in timeline.columns:
        raise ValueError(f"timeline frame missing {price_column!r}")

    base = timeline.select("ts_utc").unique(subset=["ts_utc"]).sort("ts_utc")
    relevant = (
        events
        .filter(pl.col("event_type").is_in(event_type_filter))
        .select(pl.col("ts_utc").alias("event_ts"))
        .unique(subset=["event_ts"])
        .sort("event_ts")
    )
    if relevant.height == 0:
        return base.head(0).with_columns(pl.lit(0.0).alias("value"))

    bars = (
        timeline
        .select(
            pl.col("ts_utc"),
            pl.col(price_column).cast(pl.Float64).alias("__price"),
        )
        .drop_nulls(["__price"])
        .sort("ts_utc")
    )
    if bars.height == 0:
        return base.head(0).with_columns(pl.lit(0.0).alias("value"))

    with_start = relevant.join_asof(
        bars.rename({"ts_utc": "__event_price_ts", "__price": "__event_price"}),
        left_on="event_ts",
        right_on="__event_price_ts",
        strategy="backward",
    )
    with_targets = with_start.with_columns(
        (pl.col("event_ts") + pl.duration(minutes=reaction_minutes))
        .dt.cast_time_unit("ns")
        .alias("reaction_end_ts")
    )
    with_end = with_targets.join_asof(
        bars.rename({"ts_utc": "__reaction_price_ts", "__price": "__reaction_price"}),
        left_on="reaction_end_ts",
        right_on="__reaction_price_ts",
        strategy="backward",
    )

    event_values = (
        with_end
        .filter(pl.col("__event_price").is_not_null())
        .filter(pl.col("__reaction_price").is_not_null())
        .filter(pl.col("__event_price") > 0)
        .with_columns(
            ((pl.col("__reaction_price") / pl.col("__event_price")) - 1.0)
            .cast(pl.Float64)
            .alias("__reaction_return")
        )
        .select("event_ts", "reaction_end_ts", "__reaction_return")
        .sort("reaction_end_ts")
    )
    if event_values.height == 0:
        return base.head(0).with_columns(pl.lit(0.0).alias("value"))

    joined = base.join_asof(
        event_values,
        left_on="ts_utc",
        right_on="reaction_end_ts",
        strategy="backward",
    )

    return (
        joined
        .with_columns(
            ((pl.col("ts_utc") - pl.col("event_ts")).dt.total_seconds() / 3600.0)
            .alias("__hours_since_event")
        )
        .filter(pl.col("event_ts").is_not_null())
        .filter(pl.col("__hours_since_event") >= (reaction_minutes / 60.0))
        .filter(pl.col("__hours_since_event") <= max_lookback_hours)
        .select(
            pl.col("ts_utc"),
            pl.col("__reaction_return").alias("value"),
        )
        .sort("ts_utc")
    )
