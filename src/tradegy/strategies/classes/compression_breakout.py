"""compression_breakout — enter in the direction of the first bar that
breaks out of a volatility-compressed bar.

Mechanism (N2 of signal-hunt sprint round 3): when a 1m bar's true
range collapses substantially below recent ATR (volatility cluster
contracting), the next bar's break of that compressed bar's high or
low tends to continue, because compression is information-light and a
break implies new information arriving.

Distinct from the killed Round-1/2 hypotheses:
  * H3 (`range_break_continuation`) anchored on a session-scoped OR
    level set in the morning. Compression is local and dynamic — the
    "range" is the most recent compressed bar, redefined every bar.
  * Trigger fires only after a compression bar, not on every break of
    a static level — much more selective.

State machine: track (low_of_last_compressed_bar, high_of_..._bar). On
each bar, check if it's compressed (TR < threshold × ATR). If so,
remember its extremes and wait for the next bar to break them. If the
next bar breaks, enter; if not, replace the memory with the next
compressed bar.

Parameters:
  true_range_feature_id: str
  atr_feature_id: str
  compression_ratio: float — current bar TR / ATR < this triggers
                              compression armed state
  confirmation_buffer_ticks: int — break must clear by this much
  tick_size: float
  max_attempts_per_session: int
  direction_mode: str
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


_ARMED_KEY = "armed_bar"  # tuple (high, low) | None
_LONG_ATTEMPTS_KEY = "long_attempts"
_SHORT_ATTEMPTS_KEY = "short_attempts"


@register_strategy_class("compression_breakout")
class CompressionBreakout(StrategyClass):
    id = "compression_breakout"
    version = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {
        "true_range_feature_id": {
            "type": "string", "default": "mes_true_range_1m",
        },
        "atr_feature_id": {"type": "string", "default": "mes_atr_14m"},
        "compression_ratio": {
            "type": "number", "min": 0.05, "max": 1.0, "default": 0.5,
        },
        "confirmation_buffer_ticks": {
            "type": "integer", "min": 0, "max": 50, "default": 1,
        },
        "tick_size": {"type": "number", "min": 0.0, "max": 100.0, "default": 0.25},
        "max_attempts_per_session": {
            "type": "integer", "min": 1, "max": 10, "default": 1,
        },
        "direction_mode": {"type": "string", "default": "both"},
    }
    feature_dependencies = {
        "required": ["mes_true_range_1m", "mes_atr_14m"],
        "optional": [],
    }

    def initialize(
        self, params: dict[str, Any], instrument: str, session_date: datetime
    ) -> State:
        merged = self._with_defaults(params)
        state = State(
            instrument=instrument, session_date=session_date, parameters=merged
        )
        state.extra[_ARMED_KEY] = None
        state.extra[_LONG_ATTEMPTS_KEY] = 0
        state.extra[_SHORT_ATTEMPTS_KEY] = 0
        return state

    def on_bar(
        self, state: State, bar: Bar, features: FeatureSnapshot
    ) -> list[Order]:
        params = state.parameters
        armed = state.extra.get(_ARMED_KEY)

        # Check break of an armed compressed bar BEFORE looking at this
        # bar's compression — the entry decision uses the *previous* bar's
        # extremes vs. *this* bar's close.
        order = None
        if armed is not None and state.position.is_flat:
            armed_high, armed_low = armed
            buffer = (
                int(params["confirmation_buffer_ticks"])
                * float(params["tick_size"])
            )
            mode = params["direction_mode"]
            max_a = int(params["max_attempts_per_session"])
            long_armed = (
                mode in ("long", "both")
                and state.extra.get(_LONG_ATTEMPTS_KEY, 0) < max_a
            )
            short_armed = (
                mode in ("short", "both")
                and state.extra.get(_SHORT_ATTEMPTS_KEY, 0) < max_a
            )

            if long_armed and bar.close >= armed_high + buffer:
                state.extra[_LONG_ATTEMPTS_KEY] = (
                    state.extra.get(_LONG_ATTEMPTS_KEY, 0) + 1
                )
                state.extra[_ARMED_KEY] = None
                order = Order(
                    side=Side.LONG,
                    type=OrderType.MARKET,
                    quantity=1,
                    tag=f"{self.id}:upper_break",
                )
            elif short_armed and bar.close <= armed_low - buffer:
                state.extra[_SHORT_ATTEMPTS_KEY] = (
                    state.extra.get(_SHORT_ATTEMPTS_KEY, 0) + 1
                )
                state.extra[_ARMED_KEY] = None
                order = Order(
                    side=Side.SHORT,
                    type=OrderType.MARKET,
                    quantity=1,
                    tag=f"{self.id}:lower_break",
                )

        # Whether or not we entered, recompute the armed bar from THIS
        # bar's compression read so the next bar can break it.
        tr = features.get(params["true_range_feature_id"])
        atr = features.get(params["atr_feature_id"])
        if tr is not None and atr is not None and atr > 0:
            ratio = tr / atr
            if ratio < float(params["compression_ratio"]):
                state.extra[_ARMED_KEY] = (bar.high, bar.low)
            # else: leave the previous armed bar in place — strategy
            # waits for either a break or another compression event.

        return [order] if order is not None else []

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
