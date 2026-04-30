"""LiveAdapter registry mechanics.

The registry is the lookup table that lets a YAML registry entry name a
live adapter as a string (`live.adapter: ibkr_realtime_bars_5s`). Mirrors
the transform-registry pattern in features/transforms/__init__.py.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest

from tradegy.live.base import (
    BarRow,
    LiveAdapter,
    _REGISTRY,
    get_live_adapter,
    list_live_adapters,
    register_live_adapter,
)
from tradegy.types import LiveSpec


class _NullAdapter(LiveAdapter):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    def subscribe(self, spec: LiveSpec) -> AsyncIterator[BarRow]:
        async def _gen() -> AsyncIterator[BarRow]:
            yield BarRow(
                ts_utc=datetime(2024, 1, 1, tzinfo=timezone.utc),
                open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0,
            )

        return _gen()

    def health(self) -> dict[str, Any]:
        return {"connected": True}


@pytest.fixture(autouse=True)
def _isolate_registry():
    snapshot = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)


def test_register_and_lookup() -> None:
    @register_live_adapter("test_null")
    def _factory() -> LiveAdapter:
        return _NullAdapter()

    assert "test_null" in list_live_adapters()
    adapter = get_live_adapter("test_null")
    assert isinstance(adapter, _NullAdapter)


def test_duplicate_registration_rejects() -> None:
    @register_live_adapter("test_dup")
    def _f1() -> LiveAdapter:
        return _NullAdapter()

    with pytest.raises(ValueError):
        @register_live_adapter("test_dup")
        def _f2() -> LiveAdapter:
            return _NullAdapter()


def test_unknown_lookup_raises() -> None:
    with pytest.raises(KeyError):
        get_live_adapter("not_registered_anywhere")


def test_subscribe_yields_canonical_bars() -> None:
    import asyncio

    @register_live_adapter("test_yields")
    def _factory() -> LiveAdapter:
        return _NullAdapter()

    adapter = get_live_adapter("test_yields")
    spec = LiveSpec(adapter="test_yields", params={})

    async def _drain() -> list[BarRow]:
        rows: list[BarRow] = []
        async for row in adapter.subscribe(spec):
            rows.append(row)
        return rows

    rows = asyncio.run(_drain())
    assert len(rows) == 1
    assert rows[0].close == 1.0
