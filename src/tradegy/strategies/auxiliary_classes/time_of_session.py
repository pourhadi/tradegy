"""time_of_session — boolean: current bar's session_position lies in
[lo, hi].

Per 03_strategy_class_registry.md:202 + the H2 hypothesis in the
signal-hunt sprint plan: gate entries to a window of the trading
session (e.g. 0.10–0.85 to skip both opening drive and last 30 min).

Reads from a session-position feature (defaults to `mes_session_position`
which carries fraction-through-session in [0.0, 1.0]). Bounds are
inclusive. Equivalent to `feature_range` keyed on session_position, but
named explicitly so spec YAML reads as intent rather than feature
plumbing.
"""
from __future__ import annotations

from typing import Any

from tradegy.strategies.auxiliary import (
    ConditionEvaluator,
    register_condition_evaluator,
)
from tradegy.strategies.types import Bar, FeatureSnapshot, Position


@register_condition_evaluator("time_of_session")
class TimeOfSession(ConditionEvaluator):
    id = "time_of_session"
    version = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {
        "session_position_feature_id": {
            "type": "string",
            "default": "mes_session_position",
        },
        "lo": {"type": "number", "min": 0.0, "max": 1.0, "default": 0.0},
        "hi": {"type": "number", "min": 0.0, "max": 1.0, "default": 1.0},
    }

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_parameters(params)
        lo = params.get("lo", 0.0)
        hi = params.get("hi", 1.0)
        if lo > hi:
            errors.append(f"lo ({lo}) must be <= hi ({hi})")
        return errors

    def evaluate(
        self,
        params: dict[str, Any],
        bar: Bar,
        features: FeatureSnapshot,
        position: Position,
    ) -> bool:
        feature_id = params.get(
            "session_position_feature_id", "mes_session_position"
        )
        value = features.get(feature_id)
        if value is None:
            return False
        lo = params.get("lo", 0.0)
        hi = params.get("hi", 1.0)
        return lo <= value <= hi
