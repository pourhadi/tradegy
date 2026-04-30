"""stand_down — the trivial "do nothing" strategy class.

Per 03_strategy_class_registry.md:263-267, this exists so the selection
layer can pick "stand down today" as a first-class option, not as
absence-of-selection. "Pick stand_down" is a concrete, reasoned decision
with its own rationale; "pick nothing" is harder to log, harder to
audit, and more susceptible to drift.

Implementation is one line: never emit an order. Useful primarily as
the smoke-test that exercises the strategy class registration / dispatch
plumbing end-to-end without depending on real market behavior.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from tradegy.strategies.base import StrategyClass, register_strategy_class
from tradegy.strategies.types import Bar, FeatureSnapshot, Order, State


@register_strategy_class("stand_down")
class StandDown(StrategyClass):
    id = "stand_down"
    version = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {}
    feature_dependencies = {"required": [], "optional": []}

    def initialize(
        self, params: dict[str, Any], instrument: str, session_date: datetime
    ) -> State:
        return State(
            instrument=instrument, session_date=session_date, parameters=params
        )

    def on_bar(
        self, state: State, bar: Bar, features: FeatureSnapshot
    ) -> list[Order]:
        return []
