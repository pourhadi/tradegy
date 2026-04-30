"""rolling_mean — simple rolling mean of a value series.

Reusable: ATR is the rolling_mean of true_range; smoothed-volume features
are rolling_mean of volume; etc. The transform doesn't care what the
upstream series represents.

Input slot: series (ts_utc, value), or bars when `column` is set.
Parameters:
    window_bars: int — window length in bars.
    column: str (default "value") — which column to average.
Output: ts_utc, value. First (window_bars - 1) rows are dropped (window
not yet full).
"""
from __future__ import annotations

from typing import Any

import polars as pl

from tradegy.features.transforms import register_transform


@register_transform("rolling_mean")
def rolling_mean(
    inputs: dict[str, pl.DataFrame], params: dict[str, Any]
) -> pl.DataFrame:
    if "series" not in inputs:
        raise KeyError("rolling_mean requires input named 'series'")
    df = inputs["series"].sort("ts_utc")
    window = int(params["window_bars"])
    if window < 1:
        raise ValueError("window_bars must be >= 1")
    column = params.get("column", "value")
    if column not in df.columns:
        raise ValueError(f"rolling_mean: column '{column}' not in input")

    out = (
        df.select(
            pl.col("ts_utc"),
            pl.col(column).rolling_mean(window_size=window).alias("value"),
        )
        .drop_nulls("value")
    )
    return out
