"""Resample OHLCV bars to a coarser cadence.

Distinct from `resample_ohlcv` (which aggregates raw price/size *ticks* into
OHLCV bars). This transform consumes a frame that is already OHLCV and
re-aggregates it onto a coarser bin (e.g., 5-second bars → 1-minute bars).

Aggregation rules per OHLCV semantics:
  open  = first.open in window
  high  = max.high
  low   = min.low
  close = last.close
  volume / num_trades / bid_volume / ask_volume = sum

Optional auxiliary columns (num_trades, bid_volume, ask_volume) are summed
when present and dropped from the output otherwise — this keeps the output
schema honest (no synthetic zeros).
"""
from __future__ import annotations

from typing import Any

import polars as pl

from tradegy.features.transforms import register_transform


_OPTIONAL_SUM_COLS = ("num_trades", "bid_volume", "ask_volume")


@register_transform("resample_ohlcv_bars")
def resample_ohlcv_bars(
    inputs: dict[str, pl.DataFrame], parameters: dict[str, Any]
) -> pl.DataFrame:
    bars = inputs["bars"]
    cadence = parameters["cadence"]
    label = parameters.get("bar_close_label", "right")
    closed = parameters.get("closed", "left")

    required = {"ts_utc", "open", "high", "low", "close", "volume"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"resample_ohlcv_bars: input missing columns {sorted(missing)}")

    aggs = [
        pl.col("open").first().alias("open"),
        pl.col("high").max().alias("high"),
        pl.col("low").min().alias("low"),
        pl.col("close").last().alias("close"),
        pl.col("volume").sum().alias("volume"),
    ]
    for col in _OPTIONAL_SUM_COLS:
        if col in bars.columns:
            aggs.append(pl.col(col).sum().alias(col))

    out = (
        bars.sort("ts_utc")
        .group_by_dynamic(
            "ts_utc",
            every=cadence,
            period=cadence,
            label=label,
            closed=closed,
        )
        .agg(aggs)
    )
    # group_by_dynamic with no rows produces an empty frame; preserve schema.
    return out
