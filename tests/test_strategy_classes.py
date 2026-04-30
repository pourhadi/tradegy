"""stand_down and momentum_breakout strategy classes."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tradegy.strategies import (
    Bar,
    FeatureSnapshot,
    OrderType,
    Side,
    get_strategy_class,
    list_strategy_classes,
)


def _bar(price: float = 100.0) -> Bar:
    return Bar(
        ts_utc=datetime(2024, 6, 3, 14, 30, tzinfo=timezone.utc),
        open=price, high=price + 0.5, low=price - 0.5, close=price, volume=100.0,
    )


def _features(values: dict[str, float]) -> FeatureSnapshot:
    return FeatureSnapshot(
        ts_utc=datetime(2024, 6, 3, 14, 30, tzinfo=timezone.utc),
        values=values,
    )


# ----- stand_down -----


def test_stand_down_registered() -> None:
    assert "stand_down" in list_strategy_classes()


def test_stand_down_emits_no_orders() -> None:
    cls = get_strategy_class("stand_down")
    state = cls.initialize({}, "MES", datetime(2024, 6, 3, tzinfo=timezone.utc))
    for _ in range(10):
        orders = cls.on_bar(state, _bar(), _features({}))
        assert orders == []


# ----- momentum_breakout -----


def test_momentum_breakout_registered_with_feature_deps() -> None:
    assert "momentum_breakout" in list_strategy_classes()
    cls = get_strategy_class("momentum_breakout")
    assert "mes_5m_log_returns" in cls.feature_dependencies["required"]


def test_momentum_breakout_emits_long_when_return_above_threshold() -> None:
    cls = get_strategy_class("momentum_breakout")
    state = cls.initialize(
        {"entry_threshold": 0.001},
        "MES",
        datetime(2024, 6, 3, tzinfo=timezone.utc),
    )
    orders = cls.on_bar(state, _bar(), _features({"mes_5m_log_returns": 0.002}))
    assert len(orders) == 1
    o = orders[0]
    assert o.side == Side.LONG
    assert o.type == OrderType.MARKET
    assert o.quantity == 1


def test_momentum_breakout_no_entry_below_threshold() -> None:
    cls = get_strategy_class("momentum_breakout")
    state = cls.initialize(
        {"entry_threshold": 0.001},
        "MES",
        datetime(2024, 6, 3, tzinfo=timezone.utc),
    )
    assert cls.on_bar(state, _bar(), _features({"mes_5m_log_returns": 0.0005})) == []
    # Negative return → no long.
    assert cls.on_bar(state, _bar(), _features({"mes_5m_log_returns": -0.005})) == []


def test_momentum_breakout_respects_max_attempts() -> None:
    cls = get_strategy_class("momentum_breakout")
    state = cls.initialize(
        {"entry_threshold": 0.001, "max_attempts_per_session": 1},
        "MES",
        datetime(2024, 6, 3, tzinfo=timezone.utc),
    )
    # First trigger emits an entry.
    orders = cls.on_bar(state, _bar(), _features({"mes_5m_log_returns": 0.005}))
    assert len(orders) == 1
    # Even though we're still flat (test doesn't process the fill), the
    # attempt counter prevents re-firing.
    state.position.quantity = 0  # ensure flat
    orders = cls.on_bar(state, _bar(), _features({"mes_5m_log_returns": 0.005}))
    assert orders == []


def test_momentum_breakout_no_entry_when_already_long() -> None:
    cls = get_strategy_class("momentum_breakout")
    state = cls.initialize(
        {"entry_threshold": 0.001},
        "MES",
        datetime(2024, 6, 3, tzinfo=timezone.utc),
    )
    state.position.quantity = 1  # simulate filled long
    orders = cls.on_bar(state, _bar(), _features({"mes_5m_log_returns": 0.01}))
    assert orders == []


def test_momentum_breakout_missing_feature_no_entry() -> None:
    cls = get_strategy_class("momentum_breakout")
    state = cls.initialize(
        {"entry_threshold": 0.001},
        "MES",
        datetime(2024, 6, 3, tzinfo=timezone.utc),
    )
    # Empty FeatureSnapshot → feature.get returns None → no entry.
    assert cls.on_bar(state, _bar(), _features({})) == []


def test_momentum_breakout_validate_parameters() -> None:
    cls = get_strategy_class("momentum_breakout")
    assert cls.validate_parameters({"entry_threshold": 0.001}) == []
    errs = cls.validate_parameters({"entry_threshold": 1.0})  # > 0.05 max
    assert any("max" in e for e in errs)
    errs = cls.validate_parameters({"entry_threshold": -0.001})  # < 0 min
    assert any("min" in e for e in errs)
