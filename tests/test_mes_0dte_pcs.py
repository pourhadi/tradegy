"""MES 0DTE put credit spread strategy class tests.

Real-data tests against the `mes_options_chain_ingested` fixture.
Auto-marked slow.

Coverage:

  - Strategy emits 2-leg PCS orders on at least one 0DTE session
  - Leg shape: long put + short put, quantities +1/-1, long < short
    strike, both legs same-day expiry
  - Same-day-expiry filter
  - Concentration block
  - Zero-underlying-price short-circuit
  - Subclassable via dataclass param overrides
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tradegy.options.chain import ChainSnapshot, OptionSide
from tradegy.options.databento_chain_io import iter_session_chains, make_mes_futures_price_lookup
from tradegy.options.positions import (
    LegOrder,
    MultiLegOrder,
    MultiLegPosition,
    OptionPosition,
)
from tradegy.options.strategies.mes_0dte_pcs import Mes0dtePcs


_TEST_WINDOW_START = datetime(2024, 6, 3, tzinfo=timezone.utc)
_TEST_WINDOW_END = datetime(2024, 6, 8, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def real_session_chains_for_pcs(mes_options_chain_ingested):
    lookup = make_mes_futures_price_lookup(
        root=mes_options_chain_ingested["raw_root"],
    )
    return list(iter_session_chains(
        "mes_options_chain",
        start=_TEST_WINDOW_START, end=_TEST_WINDOW_END,
        root=mes_options_chain_ingested["raw_root"],
        underlying_price_lookup=lookup,
    ))


def test_pcs_emits_orders(real_session_chains_for_pcs) -> None:
    strat = Mes0dtePcs()
    orders = []
    for snap in real_session_chains_for_pcs:
        order = strat.on_chain(snap, open_positions=())
        if order is not None:
            orders.append(order)
    assert orders


def test_pcs_order_shape(real_session_chains_for_pcs) -> None:
    strat = Mes0dtePcs()
    for snap in real_session_chains_for_pcs:
        order = strat.on_chain(snap, open_positions=())
        if order is None:
            continue
        assert isinstance(order, MultiLegOrder)
        assert len(order.legs) == 2
        long_put, short_put = order.legs
        assert (long_put.side, long_put.quantity) == (OptionSide.PUT, +1)
        assert (short_put.side, short_put.quantity) == (OptionSide.PUT, -1)
        assert long_put.strike < short_put.strike
        assert long_put.expiry == short_put.expiry == snap.ts_utc.date()
        return


def test_pcs_skips_with_zero_underlying(real_session_chains_for_pcs) -> None:
    strat = Mes0dtePcs()
    for snap in real_session_chains_for_pcs:
        stripped = ChainSnapshot(
            underlying=snap.underlying, ts_utc=snap.ts_utc,
            underlying_price=0.0, risk_free_rate=snap.risk_free_rate,
            legs=snap.legs,
        )
        assert strat.on_chain(stripped, open_positions=()) is None
        return


def test_pcs_skips_when_no_same_day_expiry(real_session_chains_for_pcs) -> None:
    strat = Mes0dtePcs()
    for snap in real_session_chains_for_pcs:
        same_day = [e for e in snap.expiries() if e == snap.ts_utc.date()]
        if not same_day:
            assert strat.on_chain(snap, open_positions=()) is None
            return
    pytest.skip("no no-same-day session in window")


def test_pcs_concentration_blocks_double_entry(real_session_chains_for_pcs) -> None:
    strat = Mes0dtePcs()
    for snap in real_session_chains_for_pcs:
        order = strat.on_chain(snap, open_positions=())
        if order is None:
            continue
        fake_legs = tuple(
            OptionPosition(
                contract_id=OptionPosition.make_contract_id(
                    "MES", l.expiry, l.strike, l.side,
                ),
                underlying="MES", expiry=l.expiry, strike=l.strike,
                side=l.side, multiplier=5, quantity=l.quantity,
                entry_price=1.0, entry_ts=snap.ts_utc,
            )
            for l in order.legs
        )
        fake_position = MultiLegPosition(
            position_id="fake-pcs-1",
            strategy_class=strat.id,
            contracts=1,
            legs=fake_legs,
            entry_ts=snap.ts_utc,
            entry_credit_per_share=1.0,
            max_loss_per_contract=125.0,
        )
        blocked = strat.on_chain(snap, open_positions=(fake_position,))
        assert blocked is None
        return
    pytest.fail("no emitting snapshot in window")


def test_pcs_dataclass_override() -> None:
    s = Mes0dtePcs(
        put_short_offset=20.0, wing_width_dollars=10.0,
        contracts=3, id="mes_0dte_pcs_20x10",
    )
    assert s.put_short_offset == 20.0
    assert s.wing_width_dollars == 10.0
    assert s.contracts == 3
    assert s.id == "mes_0dte_pcs_20x10"
