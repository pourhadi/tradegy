"""Auxiliary classes: fixed_contracts, fixed_ticks, time_stop, feature_threshold."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tradegy.strategies import (
    Bar,
    FeatureSnapshot,
    Position,
    Side,
    get_condition_evaluator,
    get_exit_class,
    get_sizing_class,
    get_stop_class,
)


def _bar(close: float = 100.0) -> Bar:
    return Bar(
        ts_utc=datetime(2024, 6, 3, 14, 30, tzinfo=timezone.utc),
        open=close, high=close + 0.5, low=close - 0.5, close=close, volume=100.0,
    )


def _features(values: dict[str, float] | None = None) -> FeatureSnapshot:
    return FeatureSnapshot(
        ts_utc=datetime(2024, 6, 3, 14, 30, tzinfo=timezone.utc),
        values=values or {},
    )


# ----- fixed_contracts -----


def test_fixed_contracts_size() -> None:
    cls = get_sizing_class("fixed_contracts")
    assert cls.size({"contracts": 3}, Side.LONG, 100.0, 99.0, 10000.0) == 3
    assert cls.size({}, Side.LONG, 100.0, 99.0, 10000.0) == 1  # default


# ----- fixed_ticks -----


def test_fixed_ticks_long_stop_below_entry() -> None:
    cls = get_stop_class("fixed_ticks")
    stop = cls.stop_price(
        {"stop_ticks": 20, "tick_size": 0.25},
        Side.LONG, 5000.0, _bar(5000.0), _features(),
    )
    assert stop == 5000.0 - 20 * 0.25  # 4995.0


def test_fixed_ticks_short_stop_above_entry() -> None:
    cls = get_stop_class("fixed_ticks")
    stop = cls.stop_price(
        {"stop_ticks": 16, "tick_size": 0.25},
        Side.SHORT, 5000.0, _bar(5000.0), _features(),
    )
    assert stop == 5000.0 + 16 * 0.25  # 5004.0


# ----- time_stop -----


def test_time_stop_no_exit_when_flat() -> None:
    cls = get_exit_class("time_stop")
    pos = Position()
    assert cls.should_exit({"max_holding_bars": 5}, pos, _bar(), _features()) is False


def test_time_stop_exits_at_max_bars() -> None:
    cls = get_exit_class("time_stop")
    pos = Position(quantity=1, avg_entry_price=100.0)
    pos.bars_since_entry = 4
    assert cls.should_exit({"max_holding_bars": 5}, pos, _bar(), _features()) is False
    pos.bars_since_entry = 5
    assert cls.should_exit({"max_holding_bars": 5}, pos, _bar(), _features()) is True


# ----- feature_threshold -----


def test_feature_threshold_gt() -> None:
    cls = get_condition_evaluator("feature_threshold")
    f = _features({"vol": 0.18})
    params = {"feature_id": "vol", "operator": "gt", "threshold": 0.15}
    assert cls.evaluate(params, _bar(), f, Position()) is True
    params["threshold"] = 0.20
    assert cls.evaluate(params, _bar(), f, Position()) is False


def test_feature_threshold_all_ops() -> None:
    cls = get_condition_evaluator("feature_threshold")
    f = _features({"x": 5.0})
    base = {"feature_id": "x", "threshold": 5.0}
    assert cls.evaluate({**base, "operator": "gt"}, _bar(), f, Position()) is False
    assert cls.evaluate({**base, "operator": "gte"}, _bar(), f, Position()) is True
    assert cls.evaluate({**base, "operator": "lt"}, _bar(), f, Position()) is False
    assert cls.evaluate({**base, "operator": "lte"}, _bar(), f, Position()) is True
    assert cls.evaluate({**base, "operator": "eq"}, _bar(), f, Position()) is True


def test_feature_threshold_missing_feature_returns_false() -> None:
    cls = get_condition_evaluator("feature_threshold")
    params = {"feature_id": "absent", "operator": "gt", "threshold": 0.0}
    assert cls.evaluate(params, _bar(), _features({}), Position()) is False


def test_feature_threshold_invalid_operator_in_validation() -> None:
    cls = get_condition_evaluator("feature_threshold")
    errs = cls.validate_parameters(
        {"feature_id": "x", "operator": "wat", "threshold": 0.0}
    )
    assert any("not in" in e for e in errs)
