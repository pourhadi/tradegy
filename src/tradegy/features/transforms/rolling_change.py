"""rolling_change — pointwise change over a fixed lag.

For each row at time t, the output is the value at t minus the value
at t - lag (where lag is measured in bars, not time). Used for "5-day
change", "20-bar momentum" and similar features. When `mode="pct"` the
output is the relative change (current / lagged - 1) instead of the
absolute difference.

Input slot: `series` (any frame with ts_utc and the chosen column).
Parameters:
    lag_bars: int — number of bars back to compare against.
    column: str (default "value") — which column.
    mode: str (default "abs") — "abs" for absolute diff, "pct" for
        relative change (current/lagged - 1).
Output: ts_utc, value (Float64).

Rows whose lag would index before the series start are dropped.
"""
from __future__ import annotations

from typing import Any

import polars as pl

from tradegy.features.transforms import register_transform


@register_transform("rolling_change")
def rolling_change(
    inputs: dict[str, pl.DataFrame], parameters: dict[str, Any]
) -> pl.DataFrame:
    if "series" not in inputs:
        raise KeyError("rolling_change requires input named 'series'")
    df = inputs["series"].sort("ts_utc")
    lag = int(parameters["lag_bars"])
    if lag < 1:
        raise ValueError("lag_bars must be >= 1")
    column = parameters.get("column", "value")
    if column not in df.columns:
        raise ValueError(f"rolling_change: column '{column}' not in input")
    mode = parameters.get("mode", "abs")
    if mode not in {"abs", "pct"}:
        raise ValueError(f"rolling_change: unsupported mode {mode!r}")

    col = pl.col(column).cast(pl.Float64)
    lagged = col.shift(lag)
    if mode == "abs":
        change = col - lagged
    else:  # pct
        change = pl.when(lagged != 0).then(col / lagged - 1).otherwise(None)

    return (
        df.select(
            pl.col("ts_utc"),
            change.alias("value"),
        )
        .drop_nulls("value")
        .sort("ts_utc")
    )
