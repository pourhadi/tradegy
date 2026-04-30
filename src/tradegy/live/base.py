"""Live data adapter protocol.

Per 02_feature_pipeline.md:42-44, the code that produces a feature in live
must be the same code that produces it in backtest. This module operationalizes
that contract at the *source* layer: every registered DataSource is paired with
a LiveAdapter that produces rows in the *same canonical schema* the historical
ingest writes to disk under data/raw/source=<id>/date=YYYY-MM-DD/.

The adapter does NOT decide the schema. The DataSource registry entry does.
The adapter's job is to subscribe to the upstream feed (e.g., IBKR), produce
rows that match the source's declared `fields`, and yield them as they arrive.

A live adapter is registered via `register_live_adapter` so YAML registry
entries can name it as a string. New adapters are a code change with tests
(same discipline as feature transforms — see
features/transforms/__init__.py).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from tradegy.types import LiveSpec


@dataclass
class BarRow:
    """A single canonical OHLCV bar row.

    Field set matches the columns the historical Sierra Chart CSV ingest
    writes (`open, high, low, close, volume, num_trades, bid_volume,
    ask_volume`), plus the UTC timestamp. Tick-aggregating adapters fill
    `num_trades` with the count of constituent ticks; bar-feed adapters
    fill it from the venue's NumberOfTrades field. Bid/ask volume may be
    None when the upstream feed does not provide signed flow.
    """

    ts_utc: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    num_trades: int | None = None
    bid_volume: float | None = None
    ask_volume: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class LiveAdapter(ABC):
    """Abstract live feed for one DataSource.

    Lifecycle:
        connect() → subscribe(spec) → ... iterator yields BarRow ... → disconnect()

    Implementations are expected to be async-friendly; the iterator is an
    AsyncIterator so consumers can `async for` over it without blocking.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Establish upstream connectivity (e.g., open IBKR socket)."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Tear down upstream connectivity. Idempotent."""

    @abstractmethod
    def subscribe(self, spec: LiveSpec) -> AsyncIterator[BarRow]:
        """Subscribe and yield rows in the source's canonical schema.

        `spec.params` carries the contract specifier and any per-source
        knobs (useRTH, exchange, currency, etc.). The adapter is
        responsible for translating those into the upstream feed's
        request shape and returning a stream of BarRow.
        """

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Connection health snapshot (connected, subscriptions, last_seen)."""


LiveAdapterFactory = Callable[[], LiveAdapter]

_REGISTRY: dict[str, LiveAdapterFactory] = {}


def register_live_adapter(
    name: str,
) -> Callable[[LiveAdapterFactory], LiveAdapterFactory]:
    """Decorator to register a LiveAdapter factory under a string name.

    The factory is a no-arg callable that returns a fresh adapter instance.
    Usually that's the adapter class itself.
    """

    def deco(factory: LiveAdapterFactory) -> LiveAdapterFactory:
        if name in _REGISTRY:
            raise ValueError(f"live adapter {name!r} already registered")
        _REGISTRY[name] = factory
        return factory

    return deco


def get_live_adapter(name: str) -> LiveAdapter:
    """Construct a fresh adapter instance for `name`."""
    if name not in _REGISTRY:
        raise KeyError(
            f"live adapter {name!r} not registered; known: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name]()


def list_live_adapters() -> list[str]:
    return sorted(_REGISTRY)
