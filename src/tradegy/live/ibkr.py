"""IBKR live adapters for the parity-contract sources.

Two adapters are registered, paired with their historical counterparts:

  ibkr_realtime_bars_5s   ↔ mes_5s_ohlcv (Sierra Chart 5s OHLCV CSV)
  ibkr_tick_aggregator_1s ↔ es_1s_ohlcv  (Sierra Chart 1s OHLCV CSV)

The 5s adapter wraps IBKR's native ``reqRealTimeBars`` (which is hard-coded
to 5-second cadence by the API). The 1s adapter wraps
``reqTickByTickData(tickType="Last")`` and aggregates the prints into 1s
OHLCV bars locally — IBKR has no native 1s real-time bar feed.

Connection lifecycle, contract resolution, and subscription teardown ARE
implemented. The actual ``subscribe()`` body that streams BarRow values is
stubbed with NotImplementedError, gated by the parity test scaffold (see
tests/integration/test_live_historical_parity.py). When the body is filled
in, no other module needs to change.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from ib_async import IB, ContFuture

from tradegy.live.base import BarRow, LiveAdapter, register_live_adapter
from tradegy.types import LiveSpec


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


class IBKRConnection:
    """Thin wrapper over ib_async.IB driven by env config.

    Reads ``IBKR_HOST`` (default 127.0.0.1), ``IBKR_PORT`` (default 7497 —
    paper TWS), ``IBKR_CLIENT_ID`` (default 17). Keeping the connection
    parameters in env (not in YAML) avoids leaking host/port into the
    registry, which is supposed to be portable across machines.
    """

    def __init__(self) -> None:
        self.host = os.environ.get("IBKR_HOST", "127.0.0.1")
        self.port = _env_int("IBKR_PORT", 7497)
        self.client_id = _env_int("IBKR_CLIENT_ID", 17)
        self.ib = IB()
        self._connected_at: datetime | None = None

    async def connect(self) -> None:
        if self.ib.isConnected():
            return
        await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
        self._connected_at = datetime.now(tz=timezone.utc)

    async def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()
        self._connected_at = None

    def health(self) -> dict[str, Any]:
        return {
            "connected": self.ib.isConnected(),
            "host": self.host,
            "port": self.port,
            "client_id": self.client_id,
            "connected_at": self._connected_at.isoformat()
            if self._connected_at
            else None,
        }


def _resolve_contract(spec: LiveSpec, ib: IB) -> ContFuture:
    """Resolve a continuous-front-month futures contract from spec.params.

    Expects ``params`` to include ``symbol`` (e.g., MES, ES); ``exchange``
    and ``currency`` default to CME / USD which is correct for the user's
    instruments. Calls ``qualifyContracts`` to populate conId etc.
    """
    symbol = spec.params.get("symbol")
    if not symbol:
        raise ValueError(f"live spec {spec.adapter!r} missing required param 'symbol'")
    exchange = spec.params.get("exchange", "CME")
    currency = spec.params.get("currency", "USD")
    contract = ContFuture(symbol, exchange=exchange, currency=currency)
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        raise RuntimeError(
            f"IBKR could not qualify ContFuture(symbol={symbol!r}, "
            f"exchange={exchange!r}); check market data subscription"
        )
    return qualified[0]


class _IBKRBaseAdapter(LiveAdapter):
    """Shared connection + contract plumbing for IBKR adapters."""

    adapter_name: str = "<override>"

    def __init__(self) -> None:
        self._conn = IBKRConnection()
        self._contract: ContFuture | None = None
        self._spec: LiveSpec | None = None

    async def connect(self) -> None:
        await self._conn.connect()

    async def disconnect(self) -> None:
        await self._conn.disconnect()
        self._contract = None
        self._spec = None

    def health(self) -> dict[str, Any]:
        h = self._conn.health()
        h["adapter"] = self.adapter_name
        h["contract_qualified"] = self._contract is not None
        if self._contract is not None:
            h["contract"] = {
                "symbol": self._contract.symbol,
                "exchange": self._contract.exchange,
                "conId": self._contract.conId,
                "lastTradeDateOrContractMonth": self._contract.lastTradeDateOrContractMonth,
            }
        return h

    def qualify(self, spec: LiveSpec) -> ContFuture:
        """Resolve and cache the contract for ``spec``. Idempotent per instance."""
        if not self._conn.ib.isConnected():
            raise RuntimeError(f"{self.adapter_name}: not connected; call connect() first")
        if self._contract is None or self._spec != spec:
            self._contract = _resolve_contract(spec, self._conn.ib)
            self._spec = spec
        return self._contract

    def subscribe(self, spec: LiveSpec) -> AsyncIterator[BarRow]:
        # Subclasses implement the upstream subscription + BarRow yielding.
        raise NotImplementedError(
            f"{self.adapter_name}.subscribe(): live capture body is deferred. "
            "See plan: study-understand-the-purring-lecun.md Phase 4 deferral."
        )


@register_live_adapter("ibkr_realtime_bars_5s")
class IBKRRealtimeBars5s(_IBKRBaseAdapter):
    """Native IBKR 5-second real-time bars.

    Wraps ``ib.reqRealTimeBars(contract, barSize=5, whatToShow="TRADES",
    useRTH=False)``. Paired with the ``mes_5s_ohlcv`` historical source.

    The 5s bar API is the only native real-time bar IBKR offers; finer
    cadences require tick aggregation.
    """

    adapter_name = "ibkr_realtime_bars_5s"

    # subscribe() inherits the NotImplementedError stub; aggregation logic
    # is straightforward (RealTimeBar → BarRow), filled in once the parity
    # test in tests/integration/ is green.


@register_live_adapter("ibkr_tick_aggregator_1s")
class IBKRTickAggregator1s(_IBKRBaseAdapter):
    """Tick-aggregating 1-second OHLCV bar producer.

    Wraps ``ib.reqTickByTickData(contract, tickType="Last")`` and aggregates
    the prints into 1-second bars locally (IBKR has no native 1s real-time
    bar feed). Paired with the ``es_1s_ohlcv`` historical source.
    """

    adapter_name = "ibkr_tick_aggregator_1s"

    # subscribe() inherits the NotImplementedError stub; the per-second
    # aggregator is bounded by the subscribe-call boundary, no shared state
    # between adapters needed.
