"""resample_ohlcv — tick prints to fixed-cadence OHLCV bars.

Inputs:
  ticks: DataFrame with columns ts_utc, price, size

Parameters:
  cadence: str — one of "1m", "5m", "1h", ... (Polars duration string)
  bar_close_label: "right" (default) — bar timestamp = end of interval

Output frame columns: ts_utc (bar close), open, high, low, close, volume.
The transform returns a frame whose rows are timestamped at the bar close;
bars whose close > now would be served only after that close, which is the
no-lookahead invariant the engine then enforces.
"""
from __future__ import annotations

from typing import Any

import polars as pl

from tradegy.features.transforms import register_transform


@register_transform("resample_ohlcv")
def resample_ohlcv(
    inputs: dict[str, pl.DataFrame], params: dict[str, Any]
) -> pl.DataFrame:
    if "ticks" not in inputs:
        raise KeyError("resample_ohlcv requires input named 'ticks'")
    cadence = params.get("cadence", "1m")
    label = params.get("bar_close_label", "right")
    if label != "right":
        raise NotImplementedError("only bar_close_label='right' supported")

    ticks = inputs["ticks"].sort("ts_utc")
    if "price" not in ticks.columns or "size" not in ticks.columns:
        raise ValueError("ticks frame must contain price and size columns")

    bars = (
        ticks.group_by_dynamic(
            "ts_utc",
            every=cadence,
            period=cadence,
            label="right",
            closed="left",
            include_boundaries=False,
        )
        .agg(
            pl.col("price").first().alias("open"),
            pl.col("price").max().alias("high"),
            pl.col("price").min().alias("low"),
            pl.col("price").last().alias("close"),
            pl.col("size").sum().cast(pl.Float64).alias("volume"),
        )
        .drop_nulls("close")
        .sort("ts_utc")
    )

    return bars
