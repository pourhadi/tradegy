"""Concrete strategy class implementations.

New classes register themselves via @register_strategy_class on import.
Add side-effect imports here so the registration runs whenever the
strategies package is loaded.
"""
from __future__ import annotations

from tradegy.strategies.classes import (  # noqa: F401
    momentum_breakout,
    range_break_continuation,
    range_break_fade,
    stand_down,
    vwap_reversion,
)
