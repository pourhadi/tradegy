"""true_range — Wilder's True Range, the per-bar range that accounts for
overnight gaps.

Definition:
    TR_t = max(
        high_t - low_t,
        |high_t - close_{t-1}|,
        |low_t  - close_{t-1}|,
    )

Captures range-based volatility separately from return-based realized
volatility. Used directly (e.g., as a stop-distance unit) and as the
input to ATR (rolling_mean of TR).

Input slot: bars (ts_utc, high, low, close).
Output: ts_utc, value = TR. First row's TR is just high - low (no prior
close to compare against — Wilder's standard convention).
"""
from __future__ import annotations

from typing import Any

import polars as pl

from tradegy.features.transforms import register_transform


@register_transform("true_range")
def true_range(
    inputs: dict[str, pl.DataFrame], params: dict[str, Any]
) -> pl.DataFrame:
    if "bars" not in inputs:
        raise KeyError("true_range requires input named 'bars'")
    bars = inputs["bars"].sort("ts_utc")
    required = {"ts_utc", "high", "low", "close"}
    missing = required - set(bars.columns)
    if missing:
        raise ValueError(f"true_range: input missing columns {sorted(missing)}")

    prev_close = pl.col("close").shift(1)
    tr = pl.max_horizontal(
        pl.col("high") - pl.col("low"),
        (pl.col("high") - prev_close).abs(),
        (pl.col("low") - prev_close).abs(),
    )
    return bars.select(pl.col("ts_utc"), tr.alias("value"))
