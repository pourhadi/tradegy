"""two_series_ratio — element-wise ratio (a / b) of two bar series.

For pairs / spread strategies that key on the price ratio between
two related instruments (MES/SPY for index basis, MES/MNQ for sector
relative strength, ZN/ZB for yield curve, etc).

Two inputs:
  * `a` — bars-shaped frame with ts_utc + a price column.
  * `b` — bars-shaped frame with ts_utc + a price column.

Parameters:
    column: str (default "close") — which column to project from each
        input. Both frames use the same column name.
    join_strategy: str (default "inner") — polars join strategy for
        aligning timestamps. "inner" drops timestamps that don't
        appear in BOTH frames (cleanest for spread strategies that
        need both sides priced).

Output: ts_utc, value (= a[column] / b[column]).
Rows where b[column] is zero or null are dropped.
"""
from __future__ import annotations

from typing import Any

import polars as pl

from tradegy.features.transforms import register_transform


@register_transform("two_series_ratio")
def two_series_ratio(
    inputs: dict[str, pl.DataFrame], parameters: dict[str, Any]
) -> pl.DataFrame:
    if "a" not in inputs:
        raise KeyError("two_series_ratio requires input named 'a'")
    if "b" not in inputs:
        raise KeyError("two_series_ratio requires input named 'b'")

    column = parameters.get("column", "close")
    join_strategy = parameters.get("join_strategy", "inner")

    a = inputs["a"]
    b = inputs["b"]
    if column not in a.columns:
        raise ValueError(f"two_series_ratio: column '{column}' not in 'a' columns")
    if column not in b.columns:
        raise ValueError(f"two_series_ratio: column '{column}' not in 'b' columns")

    # Project + align timestamps.
    a_proj = (
        a.select(
            pl.col("ts_utc"),
            pl.col(column).cast(pl.Float64).alias("__a_value"),
        )
        .sort("ts_utc")
        .unique(subset=["ts_utc"], keep="last")
    )
    b_proj = (
        b.select(
            pl.col("ts_utc"),
            pl.col(column).cast(pl.Float64).alias("__b_value"),
        )
        .sort("ts_utc")
        .unique(subset=["ts_utc"], keep="last")
    )

    joined = a_proj.join(b_proj, on="ts_utc", how=join_strategy)
    if joined.height == 0:
        return joined.select(
            pl.col("ts_utc"),
            pl.lit(0.0).alias("value"),
        ).head(0)

    out = (
        joined
        .filter(pl.col("__b_value").is_not_null())
        .filter(pl.col("__b_value") != 0.0)
        .filter(pl.col("__a_value").is_not_null())
        .select(
            pl.col("ts_utc"),
            (pl.col("__a_value") / pl.col("__b_value")).alias("value"),
        )
        .sort("ts_utc")
    )
    return out
