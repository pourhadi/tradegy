"""fixed_ticks — initial stop placed N ticks from entry.

Per 03_strategy_class_registry.md:166. Simplest stop class — useful for
strategies where stop distance is independent of recent volatility or
range. Tick size is read from spec.market_scope so different instruments
share the class.
"""
from __future__ import annotations

from typing import Any

from tradegy.strategies.auxiliary import StopClass, register_stop_class
from tradegy.strategies.types import Bar, FeatureSnapshot, Side


@register_stop_class("fixed_ticks")
class FixedTicks(StopClass):
    id = "fixed_ticks"
    version = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {
        "stop_ticks": {"type": "integer", "min": 1, "max": 200, "default": 20},
        "tick_size": {"type": "number", "min": 0.0, "max": 100.0, "default": 0.25},
    }

    def stop_price(
        self,
        params: dict[str, Any],
        side: Side,
        entry_price: float,
        bar: Bar,
        features: FeatureSnapshot,
    ) -> float:
        ticks = int(params.get("stop_ticks", 20))
        tick_size = float(params.get("tick_size", 0.25))
        offset = ticks * tick_size
        if side == Side.LONG:
            return entry_price - offset
        return entry_price + offset
