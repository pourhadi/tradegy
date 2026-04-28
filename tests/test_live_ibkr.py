"""IBKR adapter registration and protocol tests.

These do NOT require a running TWS — they verify the registration wiring,
the env-driven IBKRConnection config, and the documented stub behavior of
``subscribe()``. End-to-end live capture is exercised by the parity-test
scaffold under tests/integration/.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from tradegy.live import get_live_adapter, list_live_adapters
from tradegy.live.ibkr import IBKRConnection, IBKRRealtimeBars5s, IBKRTickAggregator1s
from tradegy.types import LiveSpec


def test_both_ibkr_adapters_registered() -> None:
    names = list_live_adapters()
    assert "ibkr_realtime_bars_5s" in names
    assert "ibkr_tick_aggregator_1s" in names


def test_factory_returns_correct_class() -> None:
    assert isinstance(get_live_adapter("ibkr_realtime_bars_5s"), IBKRRealtimeBars5s)
    assert isinstance(get_live_adapter("ibkr_tick_aggregator_1s"), IBKRTickAggregator1s)


def test_connection_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IBKR_HOST", "10.0.0.5")
    monkeypatch.setenv("IBKR_PORT", "4001")
    monkeypatch.setenv("IBKR_CLIENT_ID", "99")
    conn = IBKRConnection()
    assert conn.host == "10.0.0.5"
    assert conn.port == 4001
    assert conn.client_id == 99
    h = conn.health()
    assert h["connected"] is False
    assert h["host"] == "10.0.0.5"


def test_subscribe_stub_raises_not_implemented_with_helpful_message() -> None:
    adapter = get_live_adapter("ibkr_realtime_bars_5s")
    spec = LiveSpec(adapter="ibkr_realtime_bars_5s", params={"symbol": "MES"})

    async def _drain() -> None:
        async for _row in adapter.subscribe(spec):
            return  # pragma: no cover — should never iterate

    with pytest.raises(NotImplementedError, match="ibkr_realtime_bars_5s"):
        # subscribe() raises eagerly (it's not an async generator), so the
        # call itself raises before we'd ever async-iterate.
        adapter.subscribe(spec)


def test_qualify_without_connection_raises() -> None:
    adapter = get_live_adapter("ibkr_realtime_bars_5s")
    spec = LiveSpec(adapter="ibkr_realtime_bars_5s", params={"symbol": "MES"})
    with pytest.raises(RuntimeError, match="not connected"):
        adapter.qualify(spec)


@pytest.mark.skipif(
    os.environ.get("IBKR_HOST") is None,
    reason="TWS not reachable (set IBKR_HOST to enable)",
)
def test_real_connection_qualifies_mes() -> None:
    """Live integration check — opt-in via IBKR_HOST.

    Confirms the connect / qualify / disconnect lifecycle works against a
    real TWS or IB Gateway. Does not call subscribe().
    """
    adapter = get_live_adapter("ibkr_realtime_bars_5s")
    spec = LiveSpec(
        adapter="ibkr_realtime_bars_5s",
        params={"symbol": "MES", "exchange": "CME", "currency": "USD"},
    )

    async def _run() -> dict:
        await adapter.connect()
        try:
            adapter.qualify(spec)
            return adapter.health()
        finally:
            await adapter.disconnect()

    health = asyncio.run(_run())
    assert health["connected"] is True
    assert health["contract_qualified"] is True
    assert health["contract"]["symbol"] == "MES"
