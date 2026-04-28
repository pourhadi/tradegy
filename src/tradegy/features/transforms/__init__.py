"""Transform registry.

Per 02_feature_pipeline.md:181, transforms are registered implementations
(rolling_mean, ratio, zscore, ...). Adding a new transform is a code change
with tests, not a YAML change. A transform is a deterministic, pure
function of (input_frames, parameters) -> output_frame.

Each output_frame must contain at least:
  * ts_utc           : Datetime[ns, UTC]   — the *computation* time of the value
  * value            : Float64             — the feature value at that time

Availability latency is applied by the engine at retrieval time, not by the
transform.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import polars as pl

TransformFn = Callable[[dict[str, pl.DataFrame], dict[str, Any]], pl.DataFrame]

_REGISTRY: dict[str, TransformFn] = {}


def register_transform(name: str) -> Callable[[TransformFn], TransformFn]:
    def deco(fn: TransformFn) -> TransformFn:
        if name in _REGISTRY:
            raise ValueError(f"transform '{name}' already registered")
        _REGISTRY[name] = fn
        return fn

    return deco


def get_transform(name: str) -> TransformFn:
    if name not in _REGISTRY:
        raise KeyError(
            f"transform '{name}' not in registry; known: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name]


def list_transforms() -> list[str]:
    return sorted(_REGISTRY)


# Registration side effects. Importing this package wires up the registry.
from tradegy.features.transforms import (  # noqa: E402,F401
    log_return,
    resample_ohlcv,
    rolling_realized_vol,
)
