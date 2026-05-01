"""gap_fill_fade — fade an RTH-open gap toward the prior session close.

Mechanism (N1 of signal-hunt sprint round 3): equity-index futures tend
to mean-revert toward the prior RTH close after large overnight gaps,
because the gap is built by overnight order-flow imbalance that often
gets unwound in the early RTH session by participants buying/selling
against the gap. Enter on the first RTH bar where the open is far from
the prior close; exit at prior close (target), ATR-multiple stop on the
opposite side, or session-end / time-stop.

Distinct from the killed Round-1/2 hypotheses:
  * H1/H3 anchored on intra-session OR levels.
  * H2 anchored on intra-session VWAP.
  * THIS anchors on the **inter-session** prior-close — a different
    reference and a different time horizon (gap-fill plays out over
    the early RTH session).

The first-bar trigger uses the strategy class's own session counter:
the strategy class fires only on the first eligible bar per XNYS
session (the harness reinitializes State at every CMES session
boundary; an additional in-state guard catches the case where multiple
post-open bars are visited before the first entry decision).

Parameters:
  prior_close_feature_id: str
  gap_threshold_pct: float — minimum |open - prior_close| / prior_close
                              to trigger
  max_attempts_per_session: int
  direction_mode: str ("both" | "long" | "short")
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


_FIRED_KEY = "fired_this_session"


@register_strategy_class("gap_fill_fade")
class GapFillFade(StrategyClass):
    id = "gap_fill_fade"
    version = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {
        "prior_close_feature_id": {
            "type": "string", "default": "mes_prior_rth_close",
        },
        "gap_threshold_pct": {
            "type": "number", "min": 0.0, "max": 0.10, "default": 0.003,
        },
        "max_attempts_per_session": {
            "type": "integer", "min": 1, "max": 10, "default": 1,
        },
        "direction_mode": {"type": "string", "default": "both"},
    }
    feature_dependencies = {
        "required": ["mes_prior_rth_close"],
        "optional": [],
    }

    def initialize(
        self, params: dict[str, Any], instrument: str, session_date: datetime
    ) -> State:
        merged = self._with_defaults(params)
        state = State(
            instrument=instrument, session_date=session_date, parameters=merged
        )
        state.extra[_FIRED_KEY] = 0
        return state

    def on_bar(
        self, state: State, bar: Bar, features: FeatureSnapshot
    ) -> list[Order]:
        params = state.parameters
        if not state.position.is_flat:
            return []
        fired = state.extra.get(_FIRED_KEY, 0)
        if fired >= int(params["max_attempts_per_session"]):
            return []

        prior_close = features.get(params["prior_close_feature_id"])
        if prior_close is None or prior_close <= 0:
            # Outside RTH or first session in the data (no prior).
            return []

        gap_pct = (bar.close - prior_close) / prior_close
        threshold = float(params["gap_threshold_pct"])
        mode = params["direction_mode"]

        # Gap up: price > prior_close → fade short (target = prior_close).
        if gap_pct >= threshold and mode in ("short", "both"):
            state.extra[_FIRED_KEY] = fired + 1
            return [
                Order(
                    side=Side.SHORT,
                    type=OrderType.MARKET,
                    quantity=1,
                    tag=f"{self.id}:gap_up",
                )
            ]
        # Gap down: price < prior_close → fade long (target = prior_close).
        if gap_pct <= -threshold and mode in ("long", "both"):
            state.extra[_FIRED_KEY] = fired + 1
            return [
                Order(
                    side=Side.LONG,
                    type=OrderType.MARKET,
                    quantity=1,
                    tag=f"{self.id}:gap_down",
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
