"""feature_threshold — boolean: feature value compared to a constant.

Per 03_strategy_class_registry.md:202. The atomic condition evaluator —
"is feature X above / below threshold Y at this bar?" Every more complex
condition (regime, range, delta) eventually composes from these.

Operators: gt | gte | lt | lte | eq. The harness composes conditions
with and/or/not at the spec level, not inside any one evaluator.
"""
from __future__ import annotations

from typing import Any

from tradegy.strategies.auxiliary import (
    ConditionEvaluator,
    register_condition_evaluator,
)
from tradegy.strategies.types import Bar, FeatureSnapshot, Position


_OPS = {
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "eq": lambda a, b: a == b,
}


@register_condition_evaluator("feature_threshold")
class FeatureThreshold(ConditionEvaluator):
    id = "feature_threshold"
    version = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {
        "feature_id": {"type": "string"},
        "operator": {"type": "string", "default": "gt"},
        "threshold": {"type": "number"},
    }

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_parameters(params)
        op = params.get("operator", "gt")
        if op not in _OPS:
            errors.append(f"operator: {op!r} not in {sorted(_OPS)}")
        return errors

    def evaluate(
        self,
        params: dict[str, Any],
        bar: Bar,
        features: FeatureSnapshot,
        position: Position,
    ) -> bool:
        value = features.get(params["feature_id"])
        if value is None:
            return False
        op = _OPS[params.get("operator", "gt")]
        return bool(op(value, params["threshold"]))
