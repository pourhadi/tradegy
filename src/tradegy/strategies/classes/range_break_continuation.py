"""range_break_continuation — enter on a confirmed break of an opening-
range level with above-average volume.

Per `03_strategy_class_registry.md:289` (target Phase-1 class catalog),
H3 of the signal-hunt sprint (2026-04-30).

Mechanism: when price closes beyond the opening-range high (or low) by
≥ `confirmation_buffer_ticks` AND the breakout bar's volume z-score is
above `volume_zscore_min`, that's a volume-confirmed break — institutions
are participating. Enter in the break direction, anchored to the opposite
extreme, targeting follow-through.

The mechanism is the inverse of `range_break_fade`. If a market mostly
fades failed breaks, fade has edge; if it mostly follows confirmed breaks,
continuation has edge. The two are complementary tests of "what does
price do at the OR boundary." Neither is automatically right.

Trigger:
  * UPPER continuation long: bar.close ≥ upper + confirmation_buffer
    AND volume_zscore ≥ volume_zscore_min.
  * LOWER continuation short: symmetric.

One attempt per session per direction by default. `direction_mode` from
the spec selects long-only / short-only / both.

Parameters:
  upper_level_feature_id: str
  lower_level_feature_id: str
  volume_zscore_feature_id: str
  confirmation_buffer_ticks: int
  volume_zscore_min: float
  tick_size: float
  max_attempts_per_session: int
  direction_mode: str  ("long" | "short" | "both")
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from tradegy.strategies.base import StrategyClass, register_strategy_class
from tradegy.strategies.types import (
    Bar,
    FeatureSnapshot,
    Order,
    OrderType,
    Side,
    State,
)


_LONG_ATTEMPTS_KEY = "long_attempts"
_SHORT_ATTEMPTS_KEY = "short_attempts"


@register_strategy_class("range_break_continuation")
class RangeBreakContinuation(StrategyClass):
    id = "range_break_continuation"
    version = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {
        "upper_level_feature_id": {
            "type": "string", "default": "mes_or30_high",
        },
        "lower_level_feature_id": {
            "type": "string", "default": "mes_or30_low",
        },
        "volume_zscore_feature_id": {
            "type": "string", "default": "mes_volume_zscore_20m",
        },
        "confirmation_buffer_ticks": {
            "type": "integer", "min": 0, "max": 50, "default": 1,
        },
        "volume_zscore_min": {
            "type": "number", "min": -5.0, "max": 5.0, "default": 1.0,
        },
        "tick_size": {
            "type": "number", "min": 0.0, "max": 100.0, "default": 0.25,
        },
        "max_attempts_per_session": {
            "type": "integer", "min": 1, "max": 10, "default": 1,
        },
        "direction_mode": {
            "type": "string", "default": "both",
        },
    }
    feature_dependencies = {
        "required": [
            "mes_or30_high",
            "mes_or30_low",
            "mes_volume_zscore_20m",
        ],
        "optional": [],
    }

    def initialize(
        self, params: dict[str, Any], instrument: str, session_date: datetime
    ) -> State:
        merged = self._with_defaults(params)
        state = State(
            instrument=instrument, session_date=session_date, parameters=merged
        )
        state.extra[_LONG_ATTEMPTS_KEY] = 0
        state.extra[_SHORT_ATTEMPTS_KEY] = 0
        return state

    def on_bar(
        self, state: State, bar: Bar, features: FeatureSnapshot
    ) -> list[Order]:
        params = state.parameters
        if not state.position.is_flat:
            return []

        upper = features.get(params["upper_level_feature_id"])
        lower = features.get(params["lower_level_feature_id"])
        vz = features.get(params["volume_zscore_feature_id"])
        if upper is None or lower is None or vz is None:
            return []

        if vz < float(params["volume_zscore_min"]):
            return []

        max_attempts = int(params["max_attempts_per_session"])
        long_attempts = state.extra.get(_LONG_ATTEMPTS_KEY, 0)
        short_attempts = state.extra.get(_SHORT_ATTEMPTS_KEY, 0)
        mode = params["direction_mode"]
        long_armed = mode in ("long", "both") and long_attempts < max_attempts
        short_armed = mode in ("short", "both") and short_attempts < max_attempts

        buffer = (
            int(params["confirmation_buffer_ticks"])
            * float(params["tick_size"])
        )

        if long_armed and bar.close >= upper + buffer:
            state.extra[_LONG_ATTEMPTS_KEY] = long_attempts + 1
            return [
                Order(
                    side=Side.LONG,
                    type=OrderType.MARKET,
                    quantity=1,
                    tag=f"{self.id}:upper_break",
                )
            ]
        if short_armed and bar.close <= lower - buffer:
            state.extra[_SHORT_ATTEMPTS_KEY] = short_attempts + 1
            return [
                Order(
                    side=Side.SHORT,
                    type=OrderType.MARKET,
                    quantity=1,
                    tag=f"{self.id}:lower_break",
                )
            ]
        return []

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_parameters(params)
        mode = params.get("direction_mode", "both")
        if mode not in ("long", "short", "both"):
            errors.append(
                f"direction_mode: {mode!r} not in ('long','short','both')"
            )
        return errors

    def _with_defaults(self, params: dict[str, Any]) -> dict[str, Any]:
        out = dict(params)
        for name, spec in self.parameter_schema.items():
            if name not in out and "default" in spec:
                out[name] = spec["default"]
        return out
