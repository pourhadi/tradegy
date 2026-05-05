"""gated_entry — minimal strategy class that fires when gates pass.

Used for event-anchored strategies (pre-FOMC drift, NFP fade, etc.)
where the entry decision is made entirely by gating_conditions in the
spec, not by an in-class price/feature pattern. The class itself just
asks "am I flat AND under attempt cap?" — the WHEN comes from gates.

Gates do all the work:
  * `time_of_session` for time-of-day windows
  * `feature_range` for hours-to-next-event windows (e.g.,
    mes_hours_to_next_fomc in [0.5, 24.0] = pre-FOMC drift)
  * `feature_threshold` for regime / vol filters

This is the simplest possible class: the spec's gating_conditions are
the strategy. Useful when academic literature describes a known
window-of-edge (pre-FOMC drift, end-of-month flow, etc.) and the
mechanic is "enter at window start, exit per stop block."

Parameters:
  direction: long | short — fixed entry direction.
  max_attempts_per_session: int — cap entries per session (default 1).
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


_ATTEMPTS_KEY = "attempts_this_session"


@register_strategy_class("gated_entry")
class GatedEntry(StrategyClass):
    id = "gated_entry"
    version = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {
        "direction": {"type": "string", "default": "long"},
        "max_attempts_per_session": {
            "type": "integer", "min": 1, "max": 100, "default": 1,
        },
    }
    feature_dependencies = {
        "required": [],
        "optional": [],
    }

    def initialize(
        self, params: dict[str, Any], instrument: str, session_date: datetime
    ) -> State:
        merged = self._with_defaults(params)
        state = State(
            instrument=instrument, session_date=session_date, parameters=merged
        )
        state.extra[_ATTEMPTS_KEY] = 0
        return state

    def on_bar(
        self, state: State, bar: Bar, features: FeatureSnapshot
    ) -> list[Order]:
        params = state.parameters
        if not state.position.is_flat:
            return []
        attempts = state.extra.get(_ATTEMPTS_KEY, 0)
        if attempts >= int(params["max_attempts_per_session"]):
            return []

        direction = params.get("direction", "long")
        if direction == "long":
            side = Side.LONG
        elif direction == "short":
            side = Side.SHORT
        else:
            return []

        state.extra[_ATTEMPTS_KEY] = attempts + 1
        return [
            Order(
                side=side,
                type=OrderType.MARKET,
                quantity=1,
                tag=f"{self.id}:gated_entry",
            )
        ]

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_parameters(params)
        direction = params.get("direction", "long")
        if direction not in ("long", "short"):
            errors.append(
                f"direction: {direction!r} must be 'long' or 'short'"
            )
        return errors

    def _with_defaults(self, params: dict[str, Any]) -> dict[str, Any]:
        out = dict(params)
        for name, spec in self.parameter_schema.items():
            if name not in out and "default" in spec:
                out[name] = spec["default"]
        return out
