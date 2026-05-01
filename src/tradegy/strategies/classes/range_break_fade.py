"""range_break_fade — fade a failed breakout of an opening-range level.

Mechanism (H1 of signal-hunt sprint, 2026-04-30): when price extends
beyond the session's pre-defined opening-range high or low and then
returns inside the range within a short look-back window, the breakout
likely lacked institutional commitment. Fade — enter against the break
direction, anchored to the opposite extreme, targeting mean reversion
toward the mid-range.

Per `03_strategy_class_registry.md:289` (target Phase-1 class catalog).

The strategy keys on TWO features that together describe the OR:
  * an upper level (e.g. `mes_or30_high`) — the OR window's highest
    high, carried forward through the rest of the session
  * a lower level (e.g. `mes_or30_low`) — the OR window's lowest low

The "failed-breakout" trigger is mechanically:
  * an UPPER-fail short: price has printed above the upper level
    sometime in the recent K bars, AND the current bar's close is
    back below the upper level by ≥ `re_entry_buffer_ticks`.
  * a LOWER-fail long: symmetric on the lower side.

Look-back window for the breach is `breakout_lookback_bars` (default 5).
The state machine tracks whether a breakout has occurred in the recent
window using an in-state ring of recent (high, low) pairs; this is
session-scoped — the state resets at every session boundary.

One attempt per session per direction by default. `direction` from the
spec selects whether long-only, short-only, or both sides are armed.

Parameters:
  upper_level_feature_id: str
  lower_level_feature_id: str
  breakout_lookback_bars: int
  re_entry_buffer_ticks: int
  tick_size: float
  max_attempts_per_session: int
"""
from __future__ import annotations

from collections import deque
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


_RECENT_BARS_KEY = "recent_bars"
_LONG_ATTEMPTS_KEY = "long_attempts"
_SHORT_ATTEMPTS_KEY = "short_attempts"


@register_strategy_class("range_break_fade")
class RangeBreakFade(StrategyClass):
    id = "range_break_fade"
    version = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {
        "upper_level_feature_id": {
            "type": "string", "default": "mes_or30_high",
        },
        "lower_level_feature_id": {
            "type": "string", "default": "mes_or30_low",
        },
        "breakout_lookback_bars": {
            "type": "integer", "min": 1, "max": 60, "default": 5,
        },
        "re_entry_buffer_ticks": {
            "type": "integer", "min": 0, "max": 50, "default": 1,
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
        "required": ["mes_or30_high", "mes_or30_low"],
        "optional": [],
    }

    def initialize(
        self, params: dict[str, Any], instrument: str, session_date: datetime
    ) -> State:
        merged = self._with_defaults(params)
        state = State(
            instrument=instrument, session_date=session_date, parameters=merged
        )
        lookback = int(merged["breakout_lookback_bars"])
        # Per-session ring buffer of (high, low) pairs; session boundary
        # reinitializes State so this is naturally session-scoped.
        state.extra[_RECENT_BARS_KEY] = deque(maxlen=lookback)
        state.extra[_LONG_ATTEMPTS_KEY] = 0
        state.extra[_SHORT_ATTEMPTS_KEY] = 0
        return state

    def on_bar(
        self, state: State, bar: Bar, features: FeatureSnapshot
    ) -> list[Order]:
        params = state.parameters
        recent: deque = state.extra[_RECENT_BARS_KEY]

        # Append this bar's extremes to the ring before doing any decision
        # work — the failed-breakout test inspects the *recent window
        # including this bar*.
        recent.append((bar.high, bar.low))

        if not state.position.is_flat:
            return []

        upper = features.get(params["upper_level_feature_id"])
        lower = features.get(params["lower_level_feature_id"])
        if upper is None or lower is None:
            # OR window not yet fully formed for this session, or outside
            # RTH entirely; the OR features go null in those windows.
            return []

        long_max = int(params["max_attempts_per_session"])
        short_max = long_max
        long_attempts = state.extra.get(_LONG_ATTEMPTS_KEY, 0)
        short_attempts = state.extra.get(_SHORT_ATTEMPTS_KEY, 0)
        mode = params["direction_mode"]
        long_armed = mode in ("long", "both") and long_attempts < long_max
        short_armed = mode in ("short", "both") and short_attempts < short_max

        buffer = int(params["re_entry_buffer_ticks"]) * float(params["tick_size"])

        # Upper-fail short: any of the recent bars (excluding none — the
        # current bar counts too) printed a high > upper, AND the current
        # close has settled back below upper by at least the buffer.
        if short_armed:
            broke_up = any(h > upper for (h, _l) in recent)
            settled_back = bar.close <= upper - buffer
            if broke_up and settled_back:
                state.extra[_SHORT_ATTEMPTS_KEY] = short_attempts + 1
                return [
                    Order(
                        side=Side.SHORT,
                        type=OrderType.MARKET,
                        quantity=1,
                        tag=f"{self.id}:upper_fail",
                    )
                ]

        # Lower-fail long: mirror.
        if long_armed:
            broke_down = any(l < lower for (_h, l) in recent)
            settled_back = bar.close >= lower + buffer
            if broke_down and settled_back:
                state.extra[_LONG_ATTEMPTS_KEY] = long_attempts + 1
                return [
                    Order(
                        side=Side.LONG,
                        type=OrderType.MARKET,
                        quantity=1,
                        tag=f"{self.id}:lower_fail",
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
