from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

import tradegy.features.transforms  # noqa: F401  — register transforms
from tradegy.features.transforms import get_transform


def _timeline() -> pl.DataFrame:
    base = datetime(2024, 1, 11, 13, 25, tzinfo=timezone.utc)
    rows = []
    for i in range(46):
        ts = base + timedelta(minutes=i)
        if ts <= datetime(2024, 1, 11, 13, 30, tzinfo=timezone.utc):
            close = 100.0
        elif ts <= datetime(2024, 1, 11, 14, 0, tzinfo=timezone.utc):
            close = 101.0
        else:
            close = 102.0
        rows.append((ts, close))
    return pl.DataFrame(rows, schema=["ts_utc", "close"], orient="row").with_columns(
        pl.col("ts_utc").dt.cast_time_unit("ns").dt.replace_time_zone("UTC")
    )


def _events(event_type: str = "cpi") -> pl.DataFrame:
    return pl.DataFrame(
        {
            "ts_utc": [datetime(2024, 1, 11, 13, 30, tzinfo=timezone.utc)],
            "event_type": [event_type],
        }
    ).with_columns(
        pl.col("ts_utc").dt.cast_time_unit("ns").dt.replace_time_zone("UTC")
    )


def test_event_reaction_return_starts_after_reaction_window():
    fn = get_transform("event_reaction_return")
    out = fn(
        {"events": _events(), "timeline": _timeline()},
        {"event_type_filter": ["cpi"], "reaction_minutes": 30},
    )

    assert out.height > 0
    assert out["ts_utc"].min() == datetime(2024, 1, 11, 14, 0, tzinfo=timezone.utc)
    assert out["value"][0] == pytest.approx(0.01)


def test_event_reaction_return_filters_event_type():
    fn = get_transform("event_reaction_return")
    out = fn(
        {"events": _events("ppi"), "timeline": _timeline()},
        {"event_type_filter": ["cpi"], "reaction_minutes": 30},
    )

    assert out.height == 0


def test_event_reaction_return_respects_max_lookback():
    fn = get_transform("event_reaction_return")
    out = fn(
        {"events": _events(), "timeline": _timeline()},
        {
            "event_type_filter": ["cpi"],
            "reaction_minutes": 30,
            "max_lookback_hours": 0.55,
        },
    )

    assert out.height == 4
    assert out["ts_utc"].max() == datetime(2024, 1, 11, 14, 3, tzinfo=timezone.utc)


def test_event_reaction_return_requires_positive_reaction_minutes():
    fn = get_transform("event_reaction_return")
    with pytest.raises(ValueError, match="reaction_minutes must be > 0"):
        fn(
            {"events": _events(), "timeline": _timeline()},
            {"event_type_filter": ["cpi"], "reaction_minutes": 0},
        )
