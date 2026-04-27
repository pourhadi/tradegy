"""log_return — log return of a price series.

Inputs:
  bars: DataFrame with columns ts_utc, close (or another configured column).

Parameters:
  price_column: str (default "close")

Output: ts_utc, value (log return between consecutive rows). The first row
is dropped (no prior price to compute against). The bar's ts_utc is the
return's "ts_utc" — i.e., the return realized as of bar close.
"""
from __future__ import annotations

from typing import Any

import polars as pl

from tradegy.features.transforms import register_transform


@register_transform("log_return")
def log_return(
    inputs: dict[str, pl.DataFrame], params: dict[str, Any]
) -> pl.DataFrame:
    if "bars" not in inputs:
        raise KeyError("log_return requires input named 'bars'")
    price_col = params.get("price_column", "close")
    bars = inputs["bars"].sort("ts_utc")
    if price_col not in bars.columns:
        raise ValueError(f"price_column '{price_col}' not in bars")

    out = (
        bars.select(
            pl.col("ts_utc"),
            (pl.col(price_col).log() - pl.col(price_col).shift(1).log()).alias(
                "value"
            ),
        )
        .drop_nulls("value")
    )
    return out
