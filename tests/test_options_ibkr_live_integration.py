"""Real-IBKR integration test for the options router.

Skips automatically when an IBKR Gateway / TWS isn't reachable. Set
`IBKR_HOST` (default 127.0.0.1) and `IBKR_PORT` (default 4002 paper-
gateway) to point at the active connection. The test uses a fresh
clientId (99) to avoid collision with whatever else might be
attached.

Verifies the live contract-qualification path against IBKR's
production option-definition database — the unit-test MockIB stubs
that out, and a real-IBKR misalignment (wrong tradingClass, wrong
exchange, expired date) only surfaces here. We discovered the
SPX/SPXW tradingClass requirement through this exact test path.

Does NOT place orders. Order placement integration test belongs in
a separate phase-gated paper-trade smoke (operator-driven).
"""
from __future__ import annotations

import asyncio
import os
from datetime import date

import pytest

from tradegy.execution.ibkr_options_router import IbkrOptionsRouter
from tradegy.options.chain import ChainSnapshot, OptionSide
from tradegy.options.positions import LegOrder, MultiLegOrder


def _ibkr_reachable(host: str, port: int) -> bool:
    """Quick TCP probe; returns True if anything answers on the port.
    Cheaper than spinning up an IB client + waiting for handshake.
    """
    import socket
    try:
        sock = socket.create_connection((host, port), timeout=1.0)
        sock.close()
        return True
    except (OSError, socket.timeout):
        return False


_HOST = os.environ.get("IBKR_HOST", "127.0.0.1")
_PORT = int(os.environ.get("IBKR_PORT", "4002"))
_REACHABLE = _ibkr_reachable(_HOST, _PORT)


pytestmark = pytest.mark.skipif(
    not _REACHABLE,
    reason=f"IBKR not reachable at {_HOST}:{_PORT} — start TWS/Gateway and rerun",
)


def test_real_ibkr_qualifies_current_spx_option():
    """Connect to live (paper) IBKR and qualify a real, current
    SPXW option contract. This is the test that surfaced the
    SPX-needs-SPXW-tradingClass + CBOE-not-SMART-exchange
    requirements when first run 2026-05-03.
    """
    from datetime import datetime, timezone, timedelta
    from ib_async import IB

    async def _go():
        ib = IB()
        await asyncio.wait_for(
            ib.connectAsync(_HOST, _PORT, clientId=99, readonly=True, timeout=8),
            timeout=15,
        )
        try:
            assert ib.isConnected()
            assert ib.managedAccounts(), "no accounts on this connection"
            router = IbkrOptionsRouter(ib=ib)

            # Build a synthetic snapshot containing the leg we want
            # to qualify. The chain dataclass shape only matters for
            # the lookup_chain_leg call; the leg we care about is the
            # one passed in the order. Use a current-DTE expiry.
            today = date.today()
            # ~30 DTE on a Friday — pick the next Friday after
            # today + 30 days.
            target = today + timedelta(days=30)
            while target.weekday() != 4:  # 4 = Friday
                target += timedelta(days=1)
            spx_strike = 6800.0  # roughly ATM for SPX in 2026

            order = MultiLegOrder(
                tag="real_ibkr_qualify_test",
                contracts=1,
                legs=(
                    LegOrder(expiry=target, strike=spx_strike, side=OptionSide.PUT, quantity=-1),
                ),
            )
            # The router's contract qualifier is what we're testing.
            # Use the async variant (sync version deadlocks inside an
            # event loop).
            contract = await router._get_contract_async(
                "SPX", order.legs[0],
            )
            assert contract is not None
            assert contract.conId > 0, (
                f"qualified contract has no conId: {contract}"
            )
            assert contract.tradingClass == "SPXW", (
                f"expected SPXW tradingClass; got {contract.tradingClass!r}"
            )
            print(
                f"\n  qualified: SPX {target.strftime('%Y%m%d')} "
                f"{spx_strike}P → conId={contract.conId}, "
                f"tradingClass={contract.tradingClass}"
            )
        finally:
            ib.disconnect()

    asyncio.run(_go())
