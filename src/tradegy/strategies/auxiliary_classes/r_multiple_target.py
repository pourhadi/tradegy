"""r_multiple_target — profit target expressed as a multiple of initial R.

For a fade-style strategy where mean reversion is the entry premise,
a fixed R-multiple profit target is more natural than a trailing
stop: the target prices in "where would mean reversion satisfy us"
(typically 1.5R-2.5R from entry, near the opposite edge of the
range). Trailing stops, by contrast, assume sustained directional
movement — wrong premise for a fade.

This is a ConditionEvaluator that returns True when the position's
favorable excursion (using bar.high for longs, bar.low for shorts)
reaches `target_R × |entry - initial_stop|`. Wire it into a spec's
`exits.invalidation_conditions` to trigger the harness's exit
pathway with reason=INVALIDATION at the next bar's open.

Parameters:
    target_R: float — favorable R-multiple at which to exit.

Note: uses bar.high (long) / bar.low (short) — i.e., true intra-bar
favorable excursion, not just close. Matches how `peak_favorable`
is tracked. This means the target can be hit on a wick even if the
close has retraced; the actual exit fill happens at the NEXT bar's
open per the harness's pending_close_reason mechanic, so we don't
get magical mid-bar fills.
"""
from __future__ import annotations

from typing import Any

from tradegy.strategies.auxiliary import (
    ConditionEvaluator,
    register_condition_evaluator,
)
from tradegy.strategies.types import Bar, FeatureSnapshot, Position


@register_condition_evaluator("r_multiple_target")
class RMultipleTarget(ConditionEvaluator):
    id = "r_multiple_target"
    version = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {
        "target_R": {"type": "number", "min": 0.1, "max": 100.0, "default": 2.0},
    }

    def evaluate(
        self,
        params: dict[str, Any],
        bar: Bar,
        features: FeatureSnapshot,
        position: Position,
    ) -> bool:
        if position.is_flat:
            return False
        if position.initial_stop_price is None:
            return False
        init_r = abs(position.avg_entry_price - position.initial_stop_price)
        if init_r <= 0:
            return False
        target_R = float(params.get("target_R", 2.0))
        target_offset = target_R * init_r

        if position.quantity > 0:
            # Long: target price = entry + target_offset. Hit if bar.high
            # touches or exceeds it.
            target_px = position.avg_entry_price + target_offset
            return bar.high >= target_px
        else:
            # Short: target price = entry - target_offset. Hit if bar.low
            # touches or goes below it.
            target_px = position.avg_entry_price - target_offset
            return bar.low <= target_px
