from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import polars as pl

import tradegy.features.transforms  # noqa: F401  — register transforms
from tradegy.features.transforms import get_transform, list_transforms


def _ticks(n: int = 60, base: float = 100.0) -> pl.DataFrame:
    start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    return pl.DataFrame(
        {
            "ts_utc": [start + timedelta(seconds=i) for i in range(n)],
            "price": [base + 0.01 * i for i in range(n)],
            "size": [1] * n,
        }
    ).with_columns(pl.col("ts_utc").dt.cast_time_unit("ns").dt.replace_time_zone("UTC"))


def test_registry_enumerates_three_transforms():
    names = list_transforms()
    assert {"resample_ohlcv", "log_return", "rolling_realized_vol"} <= set(names)


def test_resample_ohlcv_groups_into_minutes():
    fn = get_transform("resample_ohlcv")
    bars = fn({"ticks": _ticks(120)}, {"cadence": "1m"})
    assert bars.height == 2
    assert set(bars.columns) == {"ts_utc", "open", "high", "low", "close", "volume"}
    assert bars.get_column("close").to_list() == [100.59, 101.19]


def test_log_return_drops_first_row():
    fn = get_transform("log_return")
    bars = pl.DataFrame(
        {
            "ts_utc": [
                datetime(2024, 1, 2, 14, 31, tzinfo=timezone.utc),
                datetime(2024, 1, 2, 14, 32, tzinfo=timezone.utc),
                datetime(2024, 1, 2, 14, 33, tzinfo=timezone.utc),
            ],
            "close": [100.0, 101.0, 100.5],
        }
    )
    out = fn({"bars": bars}, {})
    assert out.height == 2
    expected = [math.log(101.0 / 100.0), math.log(100.5 / 101.0)]
    for got, exp in zip(out.get_column("value").to_list(), expected, strict=True):
        assert math.isclose(got, exp, rel_tol=1e-12)


def test_rolling_realized_vol_window_and_annualization():
    fn = get_transform("rolling_realized_vol")
    rets = pl.DataFrame(
        {
            "ts_utc": [
                datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc) + timedelta(minutes=i)
                for i in range(10)
            ],
            "value": [0.001, -0.002, 0.0015, -0.001, 0.002, -0.0005, 0.001, -0.001, 0.0, 0.0005],
        }
    )
    out = fn({"returns": rets}, {"window_bars": 5, "bars_per_year": 98280})
    assert out.height == 6
    assert (out.get_column("value") > 0).all()
