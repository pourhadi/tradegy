"""vwap_reversion strategy class — entry / threshold / attempts tests."""
from __future__ import annotations

from datetime import datetime, timezone

from tradegy.strategies import (
    Bar,
    FeatureSnapshot,
    OrderType,
    Side,
    get_strategy_class,
    list_strategy_classes,
)


def _bar(close: float) -> Bar:
    return Bar(
        ts_utc=datetime(2024, 6, 4, 14, 30, tzinfo=timezone.utc),
        open=close, high=close + 0.5, low=close - 0.5, close=close, volume=100.0,
    )


def _features(vwap: float | None) -> FeatureSnapshot:
    values = {"mes_vwap": vwap} if vwap is not None else {}
    return FeatureSnapshot(
        ts_utc=datetime(2024, 6, 4, 14, 30, tzinfo=timezone.utc),
        values=values,
    )


def test_vwap_reversion_registered() -> None:
    assert "vwap_reversion" in list_strategy_classes()
    cls = get_strategy_class("vwap_reversion")
    assert "mes_vwap" in cls.feature_dependencies["required"]


def test_enters_long_when_close_far_below_vwap() -> None:
    cls = get_strategy_class("vwap_reversion")
    state = cls.initialize(
        {"deviation_threshold_ticks": 8, "tick_size": 0.25},
        "MES", datetime(2024, 6, 4, tzinfo=timezone.utc),
    )
    # Close 5000, VWAP 5005 → 5 points below = 20 ticks. Threshold 8 ticks
    # = 2.0 points. 5 > 2 → fire.
    orders = cls.on_bar(state, _bar(5000.0), _features(5005.0))
    assert len(orders) == 1
    assert orders[0].side == Side.LONG
    assert orders[0].type == OrderType.MARKET


def test_no_entry_when_close_near_vwap() -> None:
    cls = get_strategy_class("vwap_reversion")
    state = cls.initialize(
        {"deviation_threshold_ticks": 8, "tick_size": 0.25},
        "MES", datetime(2024, 6, 4, tzinfo=timezone.utc),
    )
    # Close 5000, VWAP 5001.5 → 1.5 points below = 6 ticks. Threshold 8 ticks → no fire.
    assert cls.on_bar(state, _bar(5000.0), _features(5001.5)) == []


def test_no_entry_when_close_above_vwap() -> None:
    """Long-only: close above VWAP never triggers."""
    cls = get_strategy_class("vwap_reversion")
    state = cls.initialize(
        {"deviation_threshold_ticks": 8, "tick_size": 0.25},
        "MES", datetime(2024, 6, 4, tzinfo=timezone.utc),
    )
    assert cls.on_bar(state, _bar(5010.0), _features(5000.0)) == []


def test_no_entry_when_already_long() -> None:
    cls = get_strategy_class("vwap_reversion")
    state = cls.initialize(
        {"deviation_threshold_ticks": 8, "tick_size": 0.25},
        "MES", datetime(2024, 6, 4, tzinfo=timezone.utc),
    )
    state.position.quantity = 1
    assert cls.on_bar(state, _bar(5000.0), _features(5005.0)) == []


def test_max_attempts_per_session_enforced() -> None:
    cls = get_strategy_class("vwap_reversion")
    state = cls.initialize(
        {"deviation_threshold_ticks": 8, "tick_size": 0.25,
         "max_attempts_per_session": 1},
        "MES", datetime(2024, 6, 4, tzinfo=timezone.utc),
    )
    # First trigger consumes the attempt.
    assert cls.on_bar(state, _bar(5000.0), _features(5005.0)) != []
    # Even after returning to flat (simulated), no more entries this session.
    state.position.quantity = 0
    assert cls.on_bar(state, _bar(5000.0), _features(5005.0)) == []


def test_no_entry_when_vwap_missing() -> None:
    cls = get_strategy_class("vwap_reversion")
    state = cls.initialize(
        {"deviation_threshold_ticks": 8, "tick_size": 0.25},
        "MES", datetime(2024, 6, 4, tzinfo=timezone.utc),
    )
    assert cls.on_bar(state, _bar(5000.0), _features(None)) == []
