"""time_stop — exit after N bars in position.

Per 03_strategy_class_registry.md:193. The minimum non-trivial exit:
forces a flat by elapsed time, regardless of price. Useful both as a
primary exit and as a backstop alongside stops/targets.
"""
from __future__ import annotations

from typing import Any

from tradegy.strategies.auxiliary import ExitClass, register_exit_class
from tradegy.strategies.types import Bar, FeatureSnapshot, Position


@register_exit_class("time_stop")
class TimeStop(ExitClass):
    id = "time_stop"
    version = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {
        "max_holding_bars": {
            "type": "integer", "min": 1, "max": 1440, "default": 30,
        },
    }

    def should_exit(
        self,
        params: dict[str, Any],
        position: Position,
        bar: Bar,
        features: FeatureSnapshot,
    ) -> bool:
        if position.is_flat:
            return False
        max_bars = int(params.get("max_holding_bars", 30))
        return position.bars_since_entry >= max_bars
