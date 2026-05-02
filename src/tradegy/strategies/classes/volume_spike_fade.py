"""volume_spike_fade — fade an extreme single-bar volume spike when the
next bar fails to extend in the same direction.

Mechanism (N3 of signal-hunt sprint round 3): a single 1m bar with
volume_zscore far above its rolling distribution often reflects a
forced flow event (margin call, liquidity provision, blow-off pop) that
is not structural. If the very next bar fails to extend the spike's
direction (i.e., the spike is "lonely"), it's likely an exhaustion
move; fade the spike's direction.

Distinct from the killed Round-1/2 hypotheses:
  * H2 anchored on intra-session VWAP (price level reference).
  * H1/H3 anchored on session-scoped OR levels.
  * THIS anchors on a **flow event** (volume z-score) and tests
    immediate follow-through. Different feature, different time
    horizon (1-bar verdict).

State machine:
  * On each bar, check if it's a "spike candidate":
    volume_zscore > threshold AND bar is directional (close near
    extreme of bar range).
  * If so, arm a fade with the candidate bar's direction.
  * On the NEXT bar, check follow-through:
    - If the next bar extends past the candidate's extreme in the
      candidate's direction, the spike was real — abort.
    - Otherwise, enter the fade in the opposite direction.

Parameters:
  volume_zscore_feature_id: str
  zscore_threshold: float
  directional_close_pct: float — close must be in the top/bottom
                                  pct of bar range to count as directional
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


_ARMED_KEY = "armed_spike"  # dict | None : {direction, high, low}
_LONG_ATTEMPTS_KEY = "long_attempts"
_SHORT_ATTEMPTS_KEY = "short_attempts"


@register_strategy_class("volume_spike_fade")
class VolumeSpikeFade(StrategyClass):
    id = "volume_spike_fade"
    version = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {
        "volume_zscore_feature_id": {
            "type": "string", "default": "mes_volume_zscore_20m",
        },
        "zscore_threshold": {
            "type": "number", "min": 1.0, "max": 10.0, "default": 2.5,
        },
        "directional_close_pct": {
            "type": "number", "min": 0.5, "max": 1.0, "default": 0.75,
        },
        "max_attempts_per_session": {
            "type": "integer", "min": 1, "max": 10, "default": 1,
        },
        "direction_mode": {"type": "string", "default": "both"},
    }
    feature_dependencies = {
        "required": ["mes_volume_zscore_20m"],
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
        order = None
        mode = params["direction_mode"]
        max_a = int(params["max_attempts_per_session"])

        # Step 1: if we have an armed spike from the previous bar, evaluate
        # follow-through on THIS bar.
        if armed is not None and state.position.is_flat:
            spike_dir = armed["direction"]
            spike_high = armed["high"]
            spike_low = armed["low"]
            if spike_dir == "up":
                # Real continuation = next bar prints a higher high than
                # the spike. Failed follow-through = next bar's high is
                # ≤ spike high → fade short.
                if bar.high > spike_high:
                    state.extra[_ARMED_KEY] = None
                else:
                    if (
                        mode in ("short", "both")
                        and state.extra.get(_SHORT_ATTEMPTS_KEY, 0) < max_a
                    ):
                        state.extra[_SHORT_ATTEMPTS_KEY] = (
                            state.extra.get(_SHORT_ATTEMPTS_KEY, 0) + 1
                        )
                        order = Order(
                            side=Side.SHORT,
                            type=OrderType.MARKET,
                            quantity=1,
                            tag=f"{self.id}:fade_up",
                        )
                    state.extra[_ARMED_KEY] = None
            else:  # spike_dir == "down"
                if bar.low < spike_low:
                    state.extra[_ARMED_KEY] = None
                else:
                    if (
                        mode in ("long", "both")
                        and state.extra.get(_LONG_ATTEMPTS_KEY, 0) < max_a
                    ):
                        state.extra[_LONG_ATTEMPTS_KEY] = (
                            state.extra.get(_LONG_ATTEMPTS_KEY, 0) + 1
                        )
                        order = Order(
                            side=Side.LONG,
                            type=OrderType.MARKET,
                            quantity=1,
                            tag=f"{self.id}:fade_down",
                        )
                    state.extra[_ARMED_KEY] = None

        # Step 2: if THIS bar is a fresh spike candidate, arm it for
        # next-bar evaluation. (We rearm even if we just entered above
        # because a chain of spikes shouldn't lose state; State already
        # forbids a second entry while position is open.)
        vz = features.get(params["volume_zscore_feature_id"])
        if vz is not None and vz >= float(params["zscore_threshold"]):
            bar_range = bar.high - bar.low
            if bar_range > 0:
                close_pct = (bar.close - bar.low) / bar_range
                threshold = float(params["directional_close_pct"])
                if close_pct >= threshold:
                    state.extra[_ARMED_KEY] = {
                        "direction": "up",
                        "high": bar.high,
                        "low": bar.low,
                    }
                elif close_pct <= 1.0 - threshold:
                    state.extra[_ARMED_KEY] = {
                        "direction": "down",
                        "high": bar.high,
                        "low": bar.low,
                    }

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
