"""rolling_zscore — z-score of a column against its rolling mean and std.

For each row, value = (col_t - mean(col_{t-window+1..t})) / std(col_{t-window+1..t}).
Used to normalize raw quantities (volume, range) so strategies key on
"how unusual is this bar relative to recent history" rather than absolute
levels.

Input slot: series (any frame containing ts_utc and the chosen column).
Parameters:
    window_bars: int — window length in bars.
    column: str (default "value") — which column to z-score. Pass
        "volume" when the input is a bars frame.
Output: ts_utc, value (the z-score). Rows with std == 0 emit null and
are dropped to avoid division-by-zero.
"""
from __future__ import annotations

from typing import Any

import polars as pl

from tradegy.features.transforms import register_transform


@register_transform("rolling_zscore")
def rolling_zscore(
    inputs: dict[str, pl.DataFrame], params: dict[str, Any]
) -> pl.DataFrame:
    if "series" not in inputs:
        raise KeyError("rolling_zscore requires input named 'series'")
    df = inputs["series"].sort("ts_utc")
    window = int(params["window_bars"])
    if window < 2:
        raise ValueError("window_bars must be >= 2 (std needs >= 2 samples)")
    column = params.get("column", "value")
    if column not in df.columns:
        raise ValueError(f"rolling_zscore: column '{column}' not in input")

    col = pl.col(column).cast(pl.Float64)
    mean = col.rolling_mean(window_size=window)
    std = col.rolling_std(window_size=window)

    out = (
        df.select(
            pl.col("ts_utc"),
            pl.when(std > 0)
            .then((col - mean) / std)
            .otherwise(None)
            .alias("value"),
        )
        .drop_nulls("value")
    )
    return out
