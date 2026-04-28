"""Strategy and auxiliary class registries.

Importing this package wires up registered classes via side-effect
imports of the implementation modules (same discipline as the transform
and live-adapter registries).
"""
from __future__ import annotations

from tradegy.strategies.auxiliary import (  # noqa: F401
    ConditionEvaluator,
    ExitClass,
    SizingClass,
    StopAdjustmentClass,
    StopClass,
    get_condition_evaluator,
    get_exit_class,
    get_sizing_class,
    get_stop_adjustment_class,
    get_stop_class,
    list_condition_evaluators,
    list_exit_classes,
    list_sizing_classes,
    list_stop_adjustment_classes,
    list_stop_classes,
    register_condition_evaluator,
    register_exit_class,
    register_sizing_class,
    register_stop_adjustment_class,
    register_stop_class,
)
from tradegy.strategies.base import (  # noqa: F401
    StrategyClass,
    get_strategy_class,
    list_strategy_classes,
    register_strategy_class,
)
from tradegy.strategies.types import (  # noqa: F401
    Bar,
    ExitReason,
    FeatureSnapshot,
    Fill,
    Order,
    OrderType,
    Position,
    Side,
    State,
    Trade,
)

# Concrete implementation modules wire themselves into the registries.
from tradegy.strategies import classes  # noqa: E402,F401  — Phase 2B

