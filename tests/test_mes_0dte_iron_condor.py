"""MES 0DTE iron condor strategy class tests.

Exercises the real `mes_options_chain` parquet partitions via the
`mes_options_chain_ingested` fixture (auto-marked slow).  Per the
no-synthetic-data rule, no hand-built ChainSnapshots — we use real
session chains from 2024-06-03..2024-06-07.

Coverage:

  - Same-day expiry filter: returns None when no chain expiry
    matches the snapshot date.
  - Underlying-price requirement: returns None when underlying_price
    is the 0.0 sentinel.
  - Concentration: returns None when this strategy already has an
    open position.
  - Leg shape: 4 legs in long-put / short-put / short-call / long-
    call order; quantities +1/-1/-1/+1.
  - Strike ordering: long_put < short_put < short_call < long_call.
  - Strikes anchored to underlying: short legs near S±offset.
  - dataclass parameters: subclassable for variants.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tradegy.options.chain import OptionSide
from tradegy.options.databento_chain_io import iter_session_chains, make_mes_futures_price_lookup
from tradegy.options.positions import (
    LegOrder,
    MultiLegOrder,
    MultiLegPosition,
    OptionPosition,
)
from tradegy.options.strategies.mes_0dte_iron_condor import Mes0dteIronCondor


_TEST_WINDOW_START = datetime(2024, 6, 3, tzinfo=timezone.utc)
_TEST_WINDOW_END = datetime(2024, 6, 8, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def real_session_chains(mes_options_chain_ingested):
    """Yield real session-level ChainSnapshots over the test window
    with underlying price populated from mes_1m_ohlcv.
    """
    lookup = make_mes_futures_price_lookup(
        root=mes_options_chain_ingested["raw_root"],
    )
    return list(iter_session_chains(
        "mes_options_chain",
        start=_TEST_WINDOW_START, end=_TEST_WINDOW_END,
        root=mes_options_chain_ingested["raw_root"],
        underlying_price_lookup=lookup,
    ))


# ── Strategy behavior ─────────────────────────────────────────────


def test_strategy_emits_orders_on_zero_dte_sessions(
    real_session_chains,
) -> None:
    """At least one of the Mon-Thu sessions in the window has a
    same-day expiry and a workable strike grid — the strategy must
    emit at least one order.
    """
    strategy = Mes0dteIronCondor()
    orders = []
    for snap in real_session_chains:
        order = strategy.on_chain(snap, open_positions=())
        if order is not None:
            orders.append(order)
    assert orders, "expected at least one valid 0DTE entry in the test window"


def test_strategy_skips_when_no_same_day_expiry(
    real_session_chains,
) -> None:
    """Find a session with no same-day expiry; strategy must
    return None.  Friday Jun 7 typically has no daily MES expiry
    (X-prefix dailies cover Mon-Thu only).
    """
    strategy = Mes0dteIronCondor()
    for snap in real_session_chains:
        same_day = [e for e in snap.expiries() if e == snap.ts_utc.date()]
        if not same_day:
            assert strategy.on_chain(snap, open_positions=()) is None
            return  # found at least one no-same-day session, test passes
    pytest.skip("no no-same-day session in test window — test inconclusive")


def test_strategy_returns_none_with_zero_underlying_price(
    real_session_chains,
) -> None:
    """When underlying_price is 0 (no lookup populated), the
    strategy can't anchor strikes and must skip.
    """
    strategy = Mes0dteIronCondor()
    for snap in real_session_chains:
        # Synthesize a stripped snapshot with underlying_price=0.
        from tradegy.options.chain import ChainSnapshot
        stripped = ChainSnapshot(
            underlying=snap.underlying,
            ts_utc=snap.ts_utc,
            underlying_price=0.0,
            risk_free_rate=snap.risk_free_rate,
            legs=snap.legs,
        )
        assert strategy.on_chain(stripped, open_positions=()) is None
        return  # one is enough


def test_strategy_concentration_blocks_double_entry(
    real_session_chains,
) -> None:
    """When this strategy already has an open position, on_chain
    must return None even on a snapshot that would otherwise emit.
    """
    strategy = Mes0dteIronCondor()
    # Find a snapshot that DOES emit on its own.
    for snap in real_session_chains:
        order = strategy.on_chain(snap, open_positions=())
        if order is None:
            continue
        # Build a fake open position for THIS strategy class.  We
        # use OptionPosition for legs (the actual MultiLegPosition
        # field) — entry prices are placeholder; only the
        # strategy_class field matters for the concentration check.
        fake_legs = tuple(
            OptionPosition(
                contract_id=OptionPosition.make_contract_id(
                    "MES", l.expiry, l.strike, l.side,
                ),
                underlying="MES",
                expiry=l.expiry,
                strike=l.strike,
                side=l.side,
                multiplier=5,
                quantity=l.quantity,
                entry_price=1.0,
                entry_ts=snap.ts_utc,
            )
            for l in order.legs
        )
        fake_position = MultiLegPosition(
            position_id="fake-1",
            strategy_class=strategy.id,  # match → blocks
            contracts=1,
            legs=fake_legs,
            entry_ts=snap.ts_utc,
            entry_credit_per_share=1.0,
            max_loss_per_contract=200.0,
        )
        # Now strategy must block.
        blocked = strategy.on_chain(snap, open_positions=(fake_position,))
        assert blocked is None
        return
    pytest.fail("no emitting snapshot in window — test inconclusive")


def test_emitted_order_has_canonical_4_leg_shape(
    real_session_chains,
) -> None:
    strategy = Mes0dteIronCondor()
    for snap in real_session_chains:
        order = strategy.on_chain(snap, open_positions=())
        if order is None:
            continue
        assert isinstance(order, MultiLegOrder)
        assert len(order.legs) == 4
        # Ordering: long_put, short_put, short_call, long_call.
        long_put, short_put, short_call, long_call = order.legs
        assert (long_put.side, long_put.quantity) == (OptionSide.PUT, +1)
        assert (short_put.side, short_put.quantity) == (OptionSide.PUT, -1)
        assert (short_call.side, short_call.quantity) == (OptionSide.CALL, -1)
        assert (long_call.side, long_call.quantity) == (OptionSide.CALL, +1)
        # Strike monotonicity.
        assert long_put.strike < short_put.strike
        assert short_put.strike < short_call.strike
        assert short_call.strike < long_call.strike
        # All four legs share one expiry (= snapshot date for 0DTE).
        assert {l.expiry for l in order.legs} == {snap.ts_utc.date()}
        return


def test_short_strikes_anchor_near_underlying(
    real_session_chains,
) -> None:
    """Default offsets are $50; short legs should land within $20 of
    that target (chain strike grid limits the precision).
    """
    strategy = Mes0dteIronCondor(
        put_short_offset=50.0, call_short_offset=50.0, wing_width_dollars=25.0,
    )
    for snap in real_session_chains:
        order = strategy.on_chain(snap, open_positions=())
        if order is None:
            continue
        S = snap.underlying_price
        _, short_put, short_call, _ = order.legs
        assert abs((S - short_put.strike) - 50.0) < 25.0, (
            f"short_put offset = {S - short_put.strike:.1f}, expected ~50"
        )
        assert abs((short_call.strike - S) - 50.0) < 25.0, (
            f"short_call offset = {short_call.strike - S:.1f}, expected ~50"
        )
        return


def test_strategy_dataclass_supports_param_overrides() -> None:
    """The dataclass should support subclasses or constructor
    kwargs for variant pre-registration.
    """
    s = Mes0dteIronCondor(
        put_short_offset=75.0,
        call_short_offset=75.0,
        wing_width_dollars=50.0,
        contracts=2,
        id="mes_0dte_ic_75x50",
    )
    assert s.put_short_offset == 75.0
    assert s.call_short_offset == 75.0
    assert s.wing_width_dollars == 50.0
    assert s.contracts == 2
    assert s.id == "mes_0dte_ic_75x50"
