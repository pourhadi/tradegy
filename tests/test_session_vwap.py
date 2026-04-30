"""session_vwap transform — verify per-session reset and weighted-mean math."""
from __future__ import annotations

from datetime import datetime, timezone

import polars as pl
import pytest

from tradegy.features.transforms import get_transform


def _utc(year, month, day, hour, minute, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _bars(rows):
    return pl.DataFrame(
        {
            "ts_utc": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [r[4] for r in rows],
        },
        schema={
            "ts_utc": pl.Datetime("ns", "UTC"),
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Int64,
        },
    )


def test_session_vwap_single_session_basic_math() -> None:
    # CMES session 2024-06-04 spans 2024-06-03 22:00 → 2024-06-04 22:00 UTC.
    # Three bars all inside that session.
    rows = [
        # typical price = (high+low+close)/3
        (_utc(2024, 6, 4, 14, 0), 100.0, 99.0, 99.5, 10),   # tp = 99.5
        (_utc(2024, 6, 4, 14, 1), 101.0, 100.0, 100.5, 20),  # tp = 100.5
        (_utc(2024, 6, 4, 14, 2), 102.0, 101.0, 101.5, 30),  # tp = 101.5
    ]
    fn = get_transform("session_vwap")
    out = fn({"bars": _bars(rows)}, {"session_calendar": "CMES"})
    vals = out.get_column("value").to_list()

    # vwap_1 = 99.5 * 10 / 10 = 99.5
    # vwap_2 = (99.5*10 + 100.5*20) / 30 = (995 + 2010) / 30 = 100.1667
    # vwap_3 = (995 + 2010 + 101.5*30) / 60 = (995 + 2010 + 3045) / 60 = 100.833
    assert vals[0] == pytest.approx(99.5, abs=1e-6)
    assert vals[1] == pytest.approx((995 + 2010) / 30, abs=1e-6)
    assert vals[2] == pytest.approx((995 + 2010 + 3045) / 60, abs=1e-6)


def test_session_vwap_resets_at_session_boundary() -> None:
    # CMES session boundary at 2024-06-04 22:00 UTC.
    # Bar A (in session 2024-06-04, 21:30 UTC): tp 100, vol 10 → vwap 100
    # Bar B (in session 2024-06-05, 22:00 UTC): tp 200, vol 5 → vwap 200
    rows = [
        (_utc(2024, 6, 4, 21, 30), 101.0, 99.0, 100.0, 10),
        (_utc(2024, 6, 4, 22, 0),  201.0, 199.0, 200.0, 5),
    ]
    fn = get_transform("session_vwap")
    out = fn({"bars": _bars(rows)}, {"session_calendar": "CMES"})
    vals = out.get_column("value").to_list()
    assert len(vals) == 2
    assert vals[0] == pytest.approx(100.0, abs=1e-6)
    # After reset, second session starts fresh: VWAP = 200, not (100*10 + 200*5)/15.
    assert vals[1] == pytest.approx(200.0, abs=1e-6)


def test_session_vwap_drops_bars_outside_any_session() -> None:
    # 2024-06-08 is Saturday — no CMES session.
    rows = [
        (_utc(2024, 6, 8, 12, 0), 100.0, 99.0, 99.5, 10),
    ]
    fn = get_transform("session_vwap")
    out = fn({"bars": _bars(rows)}, {"session_calendar": "CMES"})
    assert out.height == 0


def test_session_vwap_missing_required_column_raises() -> None:
    df = pl.DataFrame({"ts_utc": [_utc(2024, 1, 1, 0, 0)], "high": [1.0]})
    fn = get_transform("session_vwap")
    with pytest.raises(ValueError, match="missing columns"):
        fn({"bars": df}, {})
