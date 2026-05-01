"""feature_range — boolean: feature value lies within a [lo, hi] band.

Per 03_strategy_class_registry.md:202 + the H2 hypothesis in the
signal-hunt sprint plan: gate strategy entries on a feature falling
inside an absolute band (e.g. realized_vol_30m in the mid-band of its
historical distribution).

Two-sided variant of `feature_threshold` — composing two thresholds
with AND in spec YAML works but is verbose; this evaluator collapses
the common case into one entry. Bounds are inclusive on both ends.
Bound semantics:
  - lo only      => feature >= lo
  - hi only      => feature <= hi
  - lo AND hi    => lo <= feature <= hi (the regime-band case)
  - neither      => validation error (the evaluator would always fire)
"""
from __future__ import annotations

from typing import Any

from tradegy.strategies.auxiliary import (
    ConditionEvaluator,
    register_condition_evaluator,
)
from tradegy.strategies.types import Bar, FeatureSnapshot, Position


@register_condition_evaluator("feature_range")
class FeatureRange(ConditionEvaluator):
    id = "feature_range"
    version = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {
        "feature_id": {"type": "string"},
        "lo": {"type": "number", "default": float("-inf")},
        "hi": {"type": "number", "default": float("inf")},
    }

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_parameters(params)
        lo = params.get("lo", float("-inf"))
        hi = params.get("hi", float("inf"))
        if lo == float("-inf") and hi == float("inf"):
            errors.append("at least one of 'lo' or 'hi' must be set")
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
        value = features.get(params["feature_id"])
        if value is None:
            return False
        lo = params.get("lo", float("-inf"))
        hi = params.get("hi", float("inf"))
        return lo <= value <= hi
