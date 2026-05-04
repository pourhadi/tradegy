"""trailing_atr — stop trails the peak-favorable price by N × ATR.

Phase 8 originally; promoted to Phase 1 after MVP results showed the
asymmetric-exit infrastructure is the binding constraint, not regime
selection (see commit 6953eef). Implements the "good trades
aggressively exploited" mechanic from the original brainstorm.

For a long position, the stop trails `peak_favorable_price - mult * ATR`,
where `peak_favorable_price` is the highest bar.high since entry
(updated by the harness each bar). For a short, mirror: trough_low +
mult * ATR.

The trail NEVER moves the stop adversely. If the peak-derived candidate
is closer than the current stop (i.e., would loosen the stop), the
adjustment returns None — keep current. The trail only ratchets
favorably.

Optional `activation_R` parameter: if non-zero, the trail does not
activate until favorable excursion exceeds `activation_R × initial-
stop-distance`. Before activation, returns None. Reasoning: trailing
too eagerly converts winning trades into breakeven scratches; waiting
for confirmation that the trade is "real" before tightening preserves
the asymmetric R:R.

Parameters:
  atr_feature_id: str — registered ATR feature (default mes_atr_14m).
  multiplier: float — trail distance = multiplier × ATR.
  activation_R: float — trail only after this much favorable R has
    been achieved. 0.0 = always active. Typical value 0.5–1.0.
  tick_size: float — for snap-to-tick on output.
"""
from __future__ import annotations

from typing import Any

from tradegy.strategies.auxiliary import (
    StopAdjustmentClass,
    register_stop_adjustment_class,
)
from tradegy.strategies.types import Bar, FeatureSnapshot, Position, Side


@register_stop_adjustment_class("trailing_atr")
class TrailingATR(StopAdjustmentClass):
    id = "trailing_atr"
    version = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {
        "atr_feature_id": {"type": "string", "default": "mes_atr_14m"},
        "multiplier": {"type": "number", "min": 0.1, "max": 10.0, "default": 1.5},
        "activation_R": {
            "type": "number", "min": 0.0, "max": 10.0, "default": 0.0,
        },
        "tick_size": {
            "type": "number", "min": 0.0, "max": 100.0, "default": 0.25,
        },
    }

    def adjusted_stop(
        self,
        params: dict[str, Any],
        position: Position,
        bar: Bar,
        features: FeatureSnapshot,
    ) -> float | None:
        if position.is_flat:
            return None
        if position.peak_favorable_price is None:
            return None
        if position.initial_stop_price is None:
            return None

        feature_id = params.get("atr_feature_id", "mes_atr_14m")
        atr = features.get(feature_id)
        if atr is None:
            # No fallback — same posture as atr_multiple. If ATR isn't
            # available the trail can't compute; harness keeps the current
            # stop.
            return None

        multiplier = float(params.get("multiplier", 1.5))
        offset = float(atr) * multiplier

        # Activation gate: require N × initial-R favorable excursion before
        # the trail starts ratcheting. Compute initial-R as |entry - initial_stop|.
        activation_R = float(params.get("activation_R", 0.0))
        if activation_R > 0:
            init_r = abs(position.avg_entry_price - position.initial_stop_price)
            if init_r > 0:
                if position.quantity > 0:
                    favorable = position.peak_favorable_price - position.avg_entry_price
                else:
                    favorable = position.avg_entry_price - position.peak_favorable_price
                if favorable < activation_R * init_r:
                    return None

        # Compute candidate trailed stop.
        if position.quantity > 0:
            candidate = position.peak_favorable_price - offset
        else:
            candidate = position.peak_favorable_price + offset

        # Snap to tick (round toward conservative side).
        tick_size = float(params.get("tick_size", 0.25))
        if tick_size > 0:
            if position.quantity > 0:
                # Long stop: round DOWN to nearest tick (looser stop is
                # safer than tighter when snapping).
                candidate = (candidate // tick_size) * tick_size
            else:
                # Short stop: round UP.
                import math
                candidate = math.ceil(candidate / tick_size) * tick_size

        # Ratchet: only return non-None if the candidate is FAVORABLE
        # (tighter / more protective) vs the current stop. For longs:
        # candidate must be >= current_stop. For shorts: candidate must
        # be <= current_stop.
        current = position.current_stop_price
        if current is None:
            return candidate
        if position.quantity > 0:
            return candidate if candidate > current else None
        else:
            return candidate if candidate < current else None
