"""column_select — project a single named column from a source frame as
the canonical feature value.

Use case: data sources whose schema is already at the cadence we want
(e.g., daily VIX, daily econ-event flags) and that don't need OHLCV
aggregation. Instead of running them through `resample_ohlcv_bars`
(which requires a `volume` column we don't have), expose the column
directly as a `(ts_utc, value)` series.

Input slot: `frame` (any DataFrame with `ts_utc` and the chosen
column).
Parameters:
    column: str — the column to project as `value`.
Output:
    ts_utc, value (Float64).
"""
from __future__ import annotations

from typing import Any

import polars as pl

from tradegy.features.transforms import register_transform


@register_transform("column_select")
def column_select(
    inputs: dict[str, pl.DataFrame], parameters: dict[str, Any]
) -> pl.DataFrame:
    if "frame" not in inputs:
        raise KeyError("column_select requires input named 'frame'")
    df = inputs["frame"]
    column = parameters.get("column")
    if not column:
        raise ValueError("column_select: parameter 'column' is required")
    if column not in df.columns:
        raise ValueError(
            f"column_select: column '{column}' not in frame columns "
            f"{df.columns}"
        )
    if "ts_utc" not in df.columns:
        raise ValueError("column_select: frame must have 'ts_utc' column")
    return (
        df.select(
            pl.col("ts_utc"),
            pl.col(column).cast(pl.Float64).alias("value"),
        )
        .sort("ts_utc")
        .drop_nulls("value")
    )
