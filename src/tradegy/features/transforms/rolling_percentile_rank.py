"""rolling_percentile_rank — percentile rank of each value within its
trailing window.

For each row at time t, the output is the fraction of values in the
trailing `window_bars`-bar window that are strictly less than the
current value, scaled to [0, 1]. So a value at the maximum of its
window scores ~1.0; the minimum ~0.0; the median ~0.5.

Used to project quantities (VIX, realized vol, ATR) onto a regime-
relative scale that strategies can gate on consistently regardless of
absolute level shifts (e.g. "trade only when VIX is in the bottom
50% of its 252-day range").

Input slot: `series` (any frame with ts_utc and the chosen column).
Parameters:
    window_bars: int — trailing window length.
    column: str (default "value") — which column to rank.
Output: ts_utc, value (Float64 in [0, 1]).

Rows whose trailing window is shorter than `window_bars` are dropped
(the rank isn't meaningful with partial windows).

Complexity: O(n × window). Fine for daily-cadence series (e.g. VIX
~10K rows × 252 window) but slow on 1m-cadence MES (~3M rows × 6900
window = ~20B ops). For high-cadence applications, replace the inner
loop with polars' `rolling_apply` over a numpy quantile, or a sorted-
container streaming order-statistic. Tracking under Phase 1 follow-up.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from tradegy.features.transforms import register_transform


@register_transform("rolling_percentile_rank")
def rolling_percentile_rank(
    inputs: dict[str, pl.DataFrame], parameters: dict[str, Any]
) -> pl.DataFrame:
    if "series" not in inputs:
        raise KeyError("rolling_percentile_rank requires input named 'series'")
    df = inputs["series"].sort("ts_utc")
    window = int(parameters["window_bars"])
    if window < 2:
        raise ValueError("window_bars must be >= 2")
    column = parameters.get("column", "value")
    if column not in df.columns:
        raise ValueError(
            f"rolling_percentile_rank: column '{column}' not in input"
        )

    arr = df[column].cast(pl.Float64).to_numpy()
    n = arr.shape[0]
    out = np.full(n, np.nan, dtype=np.float64)

    # Sliding window with tiebreak: rank uses count-strictly-less-than +
    # half-count-equal, divided by (window - 1) so endpoints map cleanly
    # to 0 and 1. Skip the first window-1 rows where the window is
    # incomplete.
    for i in range(window - 1, n):
        wstart = i - window + 1
        win = arr[wstart : i + 1]
        cur = arr[i]
        less = np.sum(win < cur)
        equal = np.sum(win == cur)
        # Average rank for ties: less + (equal - 1) / 2 (subtract self)
        rank = less + (equal - 1) / 2.0
        out[i] = rank / (window - 1)

    result = (
        df.with_columns(pl.Series("__pct_rank", out))
        .filter(pl.col("__pct_rank").is_finite())
        .select(
            pl.col("ts_utc"),
            pl.col("__pct_rank").cast(pl.Float64).alias("value"),
        )
    )
    return result
