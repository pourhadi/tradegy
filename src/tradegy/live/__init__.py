"""Live data adapter registry.

Importing this package wires up registered adapters via side-effect imports
in the same pattern used by `tradegy.features.transforms`.
"""
from __future__ import annotations

from tradegy.live.base import (  # noqa: F401
    BarRow,
    LiveAdapter,
    get_live_adapter,
    list_live_adapters,
    register_live_adapter,
)

# Concrete adapter modules wire themselves into the registry via
# `register_live_adapter`. Each adapter module is imported here for its
# registration side effect.
from tradegy.live import ibkr  # noqa: E402,F401
