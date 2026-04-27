"""rolling_realized_vol — rolling annualized realized vol of a return series.

Inputs:
  returns: DataFrame with columns ts_utc, value (per-bar return).

Parameters:
  window_bars: int (number of bars in the rolling window; required)
  bars_per_year: int (annualization factor; e.g. 252*390 for 1-min equity bars)

Output: ts_utc, value (annualized rolling stdev of returns).

The output ts_utc is the close of the last bar in the window, so the value
is reconstructible at exactly that timestamp from history known up to it.
The first window_bars-1 outputs are dropped (insufficient history).
"""
from __future__ import annotations

import math
from typing import Any

import polars as pl

from tradegy.features.transforms import register_transform


@register_transform("rolling_realized_vol")
def rolling_realized_vol(
    inputs: dict[str, pl.DataFrame], params: dict[str, Any]
) -> pl.DataFrame:
    if "returns" not in inputs:
        raise KeyError("rolling_realized_vol requires input named 'returns'")
    window = int(params["window_bars"])
    bars_per_year = float(params["bars_per_year"])
    if window < 2:
        raise ValueError("window_bars must be >= 2")

    rets = inputs["returns"].sort("ts_utc")
    ann = math.sqrt(bars_per_year)
    out = (
        rets.with_columns(
            (pl.col("value").rolling_std(window_size=window, min_samples=window) * ann)
            .alias("rv")
        )
        .select(pl.col("ts_utc"), pl.col("rv").alias("value"))
        .drop_nulls("value")
    )
    return out
