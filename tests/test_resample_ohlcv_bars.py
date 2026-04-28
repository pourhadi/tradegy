"""resample_ohlcv_bars: aggregate finer OHLCV bars into a coarser cadence.

Distinct from resample_ohlcv (which consumes ticks). This transform's
contract is bar-in / bar-out with proper OHLCV roll-up semantics.
"""
from __future__ import annotations

from datetime import datetime, timezone

import polars as pl
import pytest

from tradegy.features.transforms import get_transform


def _bars(rows: list[tuple]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_utc": [r[0] for r in rows],
            "open": [r[1] for r in rows],
            "high": [r[2] for r in rows],
            "low": [r[3] for r in rows],
            "close": [r[4] for r in rows],
            "volume": [r[5] for r in rows],
            "num_trades": [r[6] for r in rows],
            "bid_volume": [r[7] for r in rows],
            "ask_volume": [r[8] for r in rows],
        },
        schema={
            "ts_utc": pl.Datetime("ns", "UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
            "num_trades": pl.Int64,
            "bid_volume": pl.Int64,
            "ask_volume": pl.Int64,
        },
    )


def _utc(year: int, month: int, day: int, hour: int, minute: int, second: int) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def test_aggregates_5s_bars_into_1m_bars() -> None:
    # Three 5-second bars within the 14:30:00→14:31:00 minute, then one in
    # the next minute.
    rows = [
        (_utc(2024, 6, 3, 14, 30, 5), 100.0, 101.0,  99.0, 100.5, 50, 5, 30, 20),
        (_utc(2024, 6, 3, 14, 30, 10), 100.5, 102.0,  100.5, 101.5, 60, 6, 35, 25),
        (_utc(2024, 6, 3, 14, 30, 15), 101.5, 103.0,  101.0, 102.0, 70, 7, 40, 30),
        (_utc(2024, 6, 3, 14, 31, 5), 102.0, 104.0,  101.5, 103.0, 80, 8, 45, 35),
    ]
    bars = _bars(rows)
    fn = get_transform("resample_ohlcv_bars")
    out = fn({"bars": bars}, {"cadence": "1m"})
    assert out.height == 2

    first = out.filter(pl.col("ts_utc").dt.minute() == 31).row(0, named=True)
    assert first["open"] == 100.0
    assert first["high"] == 103.0
    assert first["low"] == 99.0
    assert first["close"] == 102.0
    assert first["volume"] == 50 + 60 + 70
    assert first["num_trades"] == 18
    assert first["bid_volume"] == 105
    assert first["ask_volume"] == 75


def test_optional_columns_dropped_when_absent() -> None:
    df = pl.DataFrame(
        {
            "ts_utc": [_utc(2024, 6, 3, 14, 30, 5)],
            "open": [1.0],
            "high": [1.5],
            "low": [0.5],
            "close": [1.2],
            "volume": [10],
        },
        schema={
            "ts_utc": pl.Datetime("ns", "UTC"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
    )
    fn = get_transform("resample_ohlcv_bars")
    out = fn({"bars": df}, {"cadence": "1m"})
    assert "num_trades" not in out.columns
    assert "bid_volume" not in out.columns


def test_missing_required_column_raises() -> None:
    df = pl.DataFrame({"ts_utc": [_utc(2024, 1, 1, 0, 0, 0)], "open": [1.0]})
    fn = get_transform("resample_ohlcv_bars")
    with pytest.raises(ValueError, match="missing columns"):
        fn({"bars": df}, {"cadence": "1m"})
