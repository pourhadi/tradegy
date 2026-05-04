"""event_window_flag — boolean flag at each timeline timestamp marking
whether the current moment lies within a (pre, post) window of any
event in the events frame.

For the regime-gated strategy this is the "scheduled event quiet
window" gate: do not deploy entries within `pre_event_minutes` before
or `post_event_minutes` after a high-importance economic release.

Two inputs:
  * `events` — a frame with `ts_utc` + an `importance` column. Only
    rows whose importance is in `importance_filter` are considered.
  * `timeline` — a frame with `ts_utc`. The output emits one row per
    timeline timestamp; the timeline can be a bars feature
    (mes_1m_bars, spy_1m_bars) or any other (ts_utc, ...)-shaped
    frame at the desired output cadence.

Parameters:
    pre_event_minutes: int — how many minutes BEFORE an event activate
        the window.
    post_event_minutes: int — how many minutes AFTER an event the
        window stays active.
    importance_filter: list[str] (default ["high"]) — which importance
        levels in the events frame to count.

Output: ts_utc, value (Float64 0.0 or 1.0). 1.0 means the timeline
timestamp falls inside at least one event's [event_ts -
pre_event_minutes, event_ts + post_event_minutes] window.

Algorithm: pure polars asof joins. For each timeline ts, find the
nearest prior event and the nearest subsequent event; check if either
is within the configured window.
"""
from __future__ import annotations

from typing import Any

import polars as pl

from tradegy.features.transforms import register_transform


@register_transform("event_window_flag")
def event_window_flag(
    inputs: dict[str, pl.DataFrame], parameters: dict[str, Any]
) -> pl.DataFrame:
    if "events" not in inputs:
        raise KeyError("event_window_flag requires input named 'events'")
    if "timeline" not in inputs:
        raise KeyError("event_window_flag requires input named 'timeline'")

    pre = int(parameters["pre_event_minutes"])
    post = int(parameters["post_event_minutes"])
    if pre < 0 or post < 0:
        raise ValueError(
            "pre_event_minutes and post_event_minutes must both be >= 0"
        )

    importance_filter = parameters.get("importance_filter", ["high"])
    if not isinstance(importance_filter, list) or not importance_filter:
        raise ValueError(
            "importance_filter must be a non-empty list of strings"
        )

    events = inputs["events"]
    if "ts_utc" not in events.columns:
        raise ValueError("events frame missing 'ts_utc'")
    if "importance" not in events.columns:
        raise ValueError("events frame missing 'importance'")

    timeline = inputs["timeline"]
    if "ts_utc" not in timeline.columns:
        raise ValueError("timeline frame missing 'ts_utc'")

    # Filter events to the configured importance set, sort.
    relevant = (
        events
        .filter(pl.col("importance").is_in(importance_filter))
        .select("ts_utc")
        .unique(subset=["ts_utc"])
        .sort("ts_utc")
        .with_columns(pl.col("ts_utc").alias("event_ts"))
    )

    # Timeline sorted; emit ts_utc as the only output column initially.
    base = timeline.select("ts_utc").unique(subset=["ts_utc"]).sort("ts_utc")

    if relevant.height == 0:
        return base.with_columns(pl.lit(0.0).alias("value"))

    # Backward asof: for each timeline ts, find the nearest event with
    # event_ts <= timeline_ts. The window is active if (timeline_ts -
    # event_ts) <= post_event_minutes.
    backward = base.join_asof(
        relevant,
        on="ts_utc",
        right_on="event_ts",
        strategy="backward",
    ).rename({"event_ts": "prev_event_ts"})

    # Forward asof: for each timeline ts, find the nearest event with
    # event_ts >= timeline_ts. Active if (event_ts - timeline_ts) <=
    # pre_event_minutes.
    forward = base.join_asof(
        relevant,
        on="ts_utc",
        right_on="event_ts",
        strategy="forward",
    ).rename({"event_ts": "next_event_ts"})

    joined = backward.join(forward, on="ts_utc", how="inner")

    minute = pl.duration(minutes=1)
    post_dur = pl.duration(minutes=post)
    pre_dur = pl.duration(minutes=pre)

    # Both join_asofs may emit nulls for timeline rows beyond the events
    # range (no prior or no subsequent event). Treat null as "out of
    # window" for that side.
    in_post_window = (
        pl.col("prev_event_ts").is_not_null()
        & ((pl.col("ts_utc") - pl.col("prev_event_ts")) <= post_dur)
    )
    in_pre_window = (
        pl.col("next_event_ts").is_not_null()
        & ((pl.col("next_event_ts") - pl.col("ts_utc")) <= pre_dur)
    )

    out = joined.select(
        pl.col("ts_utc"),
        pl.when(in_post_window | in_pre_window)
        .then(1.0)
        .otherwise(0.0)
        .alias("value"),
    ).sort("ts_utc")
    # Suppress warnings about unused `minute` variable — kept for
    # readability of the duration arithmetic above.
    del minute
    return out
