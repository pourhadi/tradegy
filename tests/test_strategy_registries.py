"""Strategy and auxiliary class registry mechanics.

Verifies that the registration decorator + lookup pattern matches the
transform / live-adapter discipline: id check, duplicate-rejection,
unknown-name KeyError. The actual class implementations are tested in
their own files.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from tradegy.strategies import (
    Bar,
    ConditionEvaluator,
    ExitClass,
    FeatureSnapshot,
    Order,
    OrderType,
    Position,
    Side,
    SizingClass,
    State,
    StopAdjustmentClass,
    StopClass,
    StrategyClass,
)
from tradegy.strategies.auxiliary import (
    _cond_registry,
    _exit_registry,
    _sizing_registry,
    _stop_adj_registry,
    _stop_registry,
    register_condition_evaluator,
    register_exit_class,
    register_sizing_class,
    register_stop_adjustment_class,
    register_stop_class,
)
from tradegy.strategies.base import (
    _REGISTRY as _strategy_registry,
    register_strategy_class,
)


@pytest.fixture(autouse=True)
def _isolate_registries():
    """Snapshot every registry before each test and restore after."""
    snapshots = [
        (_strategy_registry, dict(_strategy_registry)),
        (_sizing_registry._table, dict(_sizing_registry._table)),
        (_stop_registry._table, dict(_stop_registry._table)),
        (_stop_adj_registry._table, dict(_stop_adj_registry._table)),
        (_exit_registry._table, dict(_exit_registry._table)),
        (_cond_registry._table, dict(_cond_registry._table)),
    ]
    yield
    for table, snap in snapshots:
        table.clear()
        table.update(snap)


def test_strategy_class_registration_and_lookup() -> None:
    @register_strategy_class("test_noop")
    class _Noop(StrategyClass):
        id = "test_noop"
        version = "v1"

        def initialize(self, params, instrument, session_date):
            return State(instrument=instrument, session_date=session_date, parameters=params)

        def on_bar(self, state, bar, features):
            return []

    from tradegy.strategies.base import get_strategy_class
    inst = get_strategy_class("test_noop")
    assert isinstance(inst, _Noop)
    # Each call returns a fresh instance.
    inst2 = get_strategy_class("test_noop")
    assert inst is not inst2


def test_strategy_class_id_mismatch_rejected() -> None:
    with pytest.raises(ValueError, match="class.id"):
        @register_strategy_class("other_name")
        class _Bad(StrategyClass):
            id = "wrong_name"
            version = "v1"

            def initialize(self, params, instrument, session_date):
                return State(instrument=instrument, session_date=session_date, parameters=params)

            def on_bar(self, state, bar, features):
                return []


def test_duplicate_strategy_class_rejected() -> None:
    @register_strategy_class("dup")
    class _A(StrategyClass):
        id = "dup"
        def initialize(self, params, instrument, session_date):
            return State(instrument=instrument, session_date=session_date, parameters=params)
        def on_bar(self, state, bar, features):
            return []

    with pytest.raises(ValueError, match="already registered"):
        @register_strategy_class("dup")
        class _B(StrategyClass):
            id = "dup"
            def initialize(self, params, instrument, session_date):
                return State(instrument=instrument, session_date=session_date, parameters=params)
            def on_bar(self, state, bar, features):
                return []


def test_parameter_schema_validation() -> None:
    class _Cls(StrategyClass):
        id = "v"
        parameter_schema = {
            "lookback": {"type": "integer", "min": 5, "max": 100, "default": 20},
        }
        def initialize(self, params, instrument, session_date):
            return State(instrument=instrument, session_date=session_date, parameters=params)
        def on_bar(self, state, bar, features):
            return []

    cls = _Cls()
    assert cls.validate_parameters({"lookback": 20}) == []
    errs = cls.validate_parameters({"lookback": 200})
    assert any("max" in e for e in errs)
    errs = cls.validate_parameters({"lookback": "twenty"})
    assert any("expected integer" in e for e in errs)


# Smoke tests for the auxiliary registries: each one accepts a register +
# lookup of a stub. Mostly verifies the _Registry generic plumbing.


def _stub_sizing() -> type[SizingClass]:
    @register_sizing_class("test_sizing")
    class _S(SizingClass):
        id = "test_sizing"

        def size(self, params, intended_side, entry_price, stop_price, account_equity):
            return 1
    return _S


def test_sizing_registry() -> None:
    cls = _stub_sizing()
    from tradegy.strategies.auxiliary import get_sizing_class, list_sizing_classes
    assert "test_sizing" in list_sizing_classes()
    assert isinstance(get_sizing_class("test_sizing"), cls)


def test_stop_registry() -> None:
    @register_stop_class("test_stop")
    class _S(StopClass):
        id = "test_stop"
        def stop_price(self, params, side, entry_price, bar, features):
            return entry_price - 1.0
    from tradegy.strategies.auxiliary import get_stop_class
    assert get_stop_class("test_stop").stop_price({}, Side.LONG, 100.0, None, None) == 99.0


def test_exit_registry() -> None:
    @register_exit_class("test_exit")
    class _E(ExitClass):
        id = "test_exit"
        def should_exit(self, params, position, bar, features):
            return True
    from tradegy.strategies.auxiliary import get_exit_class
    assert get_exit_class("test_exit").should_exit({}, None, None, None) is True


def test_condition_registry() -> None:
    @register_condition_evaluator("test_cond")
    class _C(ConditionEvaluator):
        id = "test_cond"
        def evaluate(self, params, bar, features, position):
            return False
    from tradegy.strategies.auxiliary import get_condition_evaluator
    assert get_condition_evaluator("test_cond").evaluate({}, None, None, None) is False


def test_unknown_lookup_raises_in_each_registry() -> None:
    from tradegy.strategies.auxiliary import (
        get_condition_evaluator,
        get_exit_class,
        get_sizing_class,
        get_stop_adjustment_class,
        get_stop_class,
    )
    from tradegy.strategies.base import get_strategy_class
    for fn in (
        get_strategy_class,
        get_sizing_class,
        get_stop_class,
        get_stop_adjustment_class,
        get_exit_class,
        get_condition_evaluator,
    ):
        with pytest.raises(KeyError):
            fn("nope_doesnt_exist")
