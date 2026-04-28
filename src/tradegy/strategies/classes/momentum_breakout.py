"""momentum_breakout — long-only continuation entry on positive momentum.

Mechanism: on each bar, read a return-horizon feature (e.g.,
``mes_5m_log_returns``); if it crosses above an entry threshold and we
are flat, emit a long market order. The harness handles stop, exit, and
sizing via the registered auxiliary classes named in the spec.

This is the simplest "real" strategy class that exercises every plumbing
joint of the harness:
  - feature consumption (FeatureSnapshot.get on a registered feature)
  - state machine: flat -> entered (state.position is_flat check)
  - order emission (single market entry)
  - parameter contract (lookback feature, threshold, max attempts/session)

Limitations (deliberate, for the MVP):
  - long only — short symmetry is a single sign flip but defer until the
    spec needs it.
  - one attempt per session by default; bumping max_attempts allows
    re-entry after a closed position.
  - no volume / regime filter — those are the job of context_conditions
    declared in the spec, evaluated by the harness, not this class.
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


@register_strategy_class("momentum_breakout")
class MomentumBreakout(StrategyClass):
    id = "momentum_breakout"
    version = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {
        "return_feature_id": {
            "type": "string",
            "default": "mes_5m_log_returns",
        },
        "entry_threshold": {
            "type": "number",
            "min": 0.0,
            "max": 0.05,
            "default": 0.001,  # 0.1% over the return horizon
        },
        "max_attempts_per_session": {
            "type": "integer",
            "min": 1,
            # Until the harness becomes session-aware (Phase 3+), the
            # whole backtest window is one logical "session", so the
            # ceiling has to be tall enough for multi-year runs. Real
            # session-per-day reset will lower this when it lands.
            "max": 100000,
            "default": 1,
        },
    }
    feature_dependencies = {
        "required": ["mes_5m_log_returns"],
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
        if attempts >= params["max_attempts_per_session"]:
            return []

        feature_id = params["return_feature_id"]
        recent_return = features.get(feature_id)
        if recent_return is None:
            return []

        if recent_return > params["entry_threshold"]:
            state.extra[_ATTEMPTS_KEY] = attempts + 1
            return [
                Order(
                    side=Side.LONG,
                    type=OrderType.MARKET,
                    quantity=1,
                    tag=f"{self.id}:entry",
                )
            ]
        return []

    def _with_defaults(self, params: dict[str, Any]) -> dict[str, Any]:
        out = dict(params)
        for name, spec in self.parameter_schema.items():
            if name not in out and "default" in spec:
                out[name] = spec["default"]
        return out
