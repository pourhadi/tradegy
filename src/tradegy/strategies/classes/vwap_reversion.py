"""vwap_reversion — long-only fade of intraday extension below session VWAP.

Mechanism: when the current bar's close is significantly below the
session VWAP, the price has likely overshot intraday equilibrium and
participants tend to mean-revert. Enter long; let the spec's stop /
exit blocks handle risk and exit timing.

Per 03_strategy_class_registry.md the canonical version is bidirectional
(also short on extension above VWAP). The MVP implements the long
direction only; the short side is a sign-flip away and lands when a
spec asks for it.

Parameters:
  vwap_feature_id: str — registered feature delivering session VWAP.
                          Default ``mes_vwap``.
  deviation_threshold_ticks: int — close must be at least this many
                                   ticks below VWAP to trigger.
  tick_size: float — instrument tick size for converting ticks to price.
  max_attempts_per_session: int — entries allowed per CMES session.

Limitations (deliberate, MVP):
  - long-only.
  - no time-of-day gate — context_conditions block in the spec is the
    intended place for "don't enter in the last 30 minutes" semantics.
  - no volatility scaling on the threshold — fixed ticks for now.
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


@register_strategy_class("vwap_reversion")
class VwapReversion(StrategyClass):
    id = "vwap_reversion"
    version = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {
        "vwap_feature_id": {"type": "string", "default": "mes_vwap"},
        "deviation_threshold_ticks": {
            "type": "integer", "min": 1, "max": 200, "default": 8,
        },
        "tick_size": {
            "type": "number", "min": 0.0, "max": 100.0, "default": 0.25,
        },
        "max_attempts_per_session": {
            "type": "integer", "min": 1, "max": 100, "default": 1,
        },
    }
    feature_dependencies = {
        "required": ["mes_vwap"],
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

        vwap = features.get(params["vwap_feature_id"])
        if vwap is None:
            return []

        threshold = params["deviation_threshold_ticks"] * params["tick_size"]
        if bar.close <= vwap - threshold:
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
