"""Live/historical parity test scaffold.

The live half of the parity contract: for every DataSource that declares a
`live` adapter, the rows the adapter yields in real time must match the
canonical row schema the historical ingest writes — same columns, same
dtypes, same units, same UTC convention.

This file is the place that contract is *enforced*. Today the IBKR
subscribe() bodies raise NotImplementedError (see live/ibkr.py — Phase 4
deferral). When those bodies land, the tests in this file gate the merge:
no live capture without parity to the historical schema.

Skip discipline:
- No IBKR_HOST in env → skipped (default for CI and local dev without TWS).
- IBKR_HOST set + subscribe() still stubbed → fails with NotImplementedError;
  that's the gate.
"""
from __future__ import annotations

import asyncio
import os
from datetime import timedelta

import pytest

from tradegy.live import get_live_adapter
from tradegy.registry.loader import load_data_source


_TWS_AVAILABLE = os.environ.get("IBKR_HOST") is not None


# Canonical OHLCV row schema produced by the historical Sierra Chart ingest.
# Live adapters MUST match this exactly (column names) so the engine sees a
# single uniform shape under data/raw/source=<id>/.
_REQUIRED_BAR_FIELDS: set[str] = {
    "ts_utc",
    "open",
    "high",
    "low",
    "close",
    "volume",
}


PARITY_SOURCES = ["mes_5s_ohlcv", "es_1s_ohlcv"]


@pytest.mark.parametrize("source_id", PARITY_SOURCES)
@pytest.mark.skipif(not _TWS_AVAILABLE, reason="TWS not reachable (set IBKR_HOST)")
def test_live_adapter_capture_matches_canonical_schema(source_id: str) -> None:
    """Capture a small handful of bars from the live adapter and assert the
    BarRow schema is field-compatible with the historical canonical row.

    Activates only with a running TWS/Gateway. Until the live adapter
    bodies are implemented, this test fails-loudly (NotImplementedError) —
    that failure is the gate that says "the live half of the parity
    contract is incomplete; do not merge."
    """
    source = load_data_source(source_id)
    assert source.live is not None, f"{source_id} missing live block"

    adapter = get_live_adapter(source.live.adapter)

    async def _drain(n: int) -> list:
        await adapter.connect()
        rows: list = []
        try:
            async for row in adapter.subscribe(source.live):
                rows.append(row)
                if len(rows) >= n:
                    break
        finally:
            await adapter.disconnect()
        return rows

    rows = asyncio.run(_drain(3))
    assert rows, f"{source_id}: live adapter produced no rows"

    first = rows[0]
    # BarRow declared fields cover the canonical set; this assertion fails
    # if a future BarRow drops a field we depend on.
    for field in _REQUIRED_BAR_FIELDS:
        assert hasattr(first, field), (
            f"{source_id}: BarRow missing canonical field {field!r}"
        )

    # ts_utc must be tz-aware UTC.
    assert first.ts_utc.tzinfo is not None, "ts_utc must be tz-aware"
    assert first.ts_utc.utcoffset() == timedelta(0), "ts_utc must be UTC"
