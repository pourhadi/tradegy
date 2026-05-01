"""atr_multiple — initial stop placed N × ATR from entry.

Volatility-scaled alternative to `fixed_ticks`. The stop distance adapts
to current realized volatility, so a strategy doesn't get knocked out
by ordinary intraday range during high-vol regimes nor pay full-ATR
slippage during quiet ones.

Reads an ATR feature (default `mes_atr_14m`) from the FeatureSnapshot
at the entry bar. If the feature is unavailable (None — feature not
yet warmed up at this bar), this raises a ValueError rather than
silently falling back. Per project rules: no fallback logic.

Per `03_strategy_class_registry.md:297` (target Phase-1 stop class
catalog). Implemented 2026-05-01 as the structural change for the
second signal-hunt sprint after fixed-tick variants killed the first.

Parameters:
  atr_feature_id: str — registered ATR feature name (default mes_atr_14m).
  multiplier: float — stop distance = multiplier × ATR.
  max_distance_ticks: int — runtime cap; if ATR-derived distance exceeds
                            this many ticks, raises ValueError so a regime
                            spike can't produce a runaway stop.
  tick_size: float — instrument tick size (for the cap conversion).
"""
from __future__ import annotations

from typing import Any

from tradegy.strategies.auxiliary import StopClass, register_stop_class
from tradegy.strategies.types import Bar, FeatureSnapshot, Side


@register_stop_class("atr_multiple")
class AtrMultiple(StopClass):
    id = "atr_multiple"
    version = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {
        "atr_feature_id": {"type": "string", "default": "mes_atr_14m"},
        "multiplier": {"type": "number", "min": 0.1, "max": 10.0, "default": 2.0},
        "max_distance_ticks": {
            "type": "integer", "min": 1, "max": 1000, "default": 100,
        },
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
        feature_id = params.get("atr_feature_id", "mes_atr_14m")
        atr = features.get(feature_id)
        if atr is None:
            raise ValueError(
                f"atr_multiple: feature {feature_id!r} unavailable at "
                f"entry bar {bar.ts_utc.isoformat()} — no fallback. "
                "Either add the feature to the harness panel or warm "
                "the strategy past the ATR window."
            )
        multiplier = float(params.get("multiplier", 2.0))
        offset = float(atr) * multiplier
        max_ticks = int(params.get("max_distance_ticks", 100))
        tick_size = float(params.get("tick_size", 0.25))
        max_offset = max_ticks * tick_size
        if offset > max_offset:
            raise ValueError(
                f"atr_multiple: computed offset {offset:.2f} exceeds "
                f"max_distance_ticks {max_ticks} ({max_offset:.2f} px) "
                f"at {bar.ts_utc.isoformat()}; widen the cap or skip "
                "the trade in a gating condition."
            )
        if side == Side.LONG:
            return entry_price - offset
        return entry_price + offset
