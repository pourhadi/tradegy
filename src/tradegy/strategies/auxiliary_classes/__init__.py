"""Concrete auxiliary class implementations (one per axis for the MVP).

Future implementations register themselves on import; add side-effect
imports here so loading the strategies package wires them all in.
"""
from __future__ import annotations

from tradegy.strategies.auxiliary_classes import (  # noqa: F401
    feature_range,
    feature_threshold,
    fixed_contracts,
    fixed_ticks,
    time_of_session,
    time_stop,
)
