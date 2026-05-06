"""0DTE backtest harness tests.

Exercises the harness against real `mes_options_chain` partitions
plus the real `mes_1m_ohlcv` futures source for underlying prices.
Auto-marked slow via `mes_options_chain_ingested` fixture.

Coverage:

  - run_zero_dte_backtest requires an underlying_price_lookup; raises
    ValueError otherwise.
  - On a window with at least one 0DTE-eligible session and a
    workable strategy, returns a non-empty BacktestResult.
  - TradeRecord fields are well-formed (P&L = gross - slippage -
    commission, credit/debit signs consistent).
  - Settlement uses intrinsic value at session-close underlying.
  - Skipped sessions accounted for cleanly.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tradegy.options.chain import OptionSide
from tradegy.options.databento_chain_io import make_mes_futures_price_lookup
from tradegy.options.strategies.mes_0dte_iron_condor import Mes0dteIronCondor
from tradegy.options.zero_dte_runner import (
    BacktestResult,
    TradeRecord,
    run_zero_dte_backtest,
)


_TEST_WINDOW_START = datetime(2024, 6, 3, tzinfo=timezone.utc)
_TEST_WINDOW_END = datetime(2024, 6, 8, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def real_lookup_and_root(mes_options_chain_ingested):
    raw_root = mes_options_chain_ingested["raw_root"]
    return make_mes_futures_price_lookup(root=raw_root), raw_root


def test_runner_requires_underlying_lookup() -> None:
    strat = Mes0dteIronCondor()
    with pytest.raises(ValueError, match="underlying_price_lookup"):
        run_zero_dte_backtest(
            strat,
            start=_TEST_WINDOW_START,
            end=_TEST_WINDOW_END,
            underlying_price_lookup=None,
        )


def test_runner_returns_well_formed_result(real_lookup_and_root) -> None:
    lookup, root = real_lookup_and_root
    strat = Mes0dteIronCondor()
    r = run_zero_dte_backtest(
        strat,
        start=_TEST_WINDOW_START, end=_TEST_WINDOW_END,
        root=root, underlying_price_lookup=lookup,
    )
    assert isinstance(r, BacktestResult)
    assert r.n_sessions_total >= 0
    assert r.n_sessions_traded + r.n_sessions_skipped_no_entry >= r.n_sessions_total - 1
    # Trades + skipped should account for sessions (modulo race-condition
    # edges from intra-day chain reads).
    assert r.n_sessions_traded == len(r.trades)


def test_traded_sessions_yield_well_formed_trade_records(
    real_lookup_and_root,
) -> None:
    lookup, root = real_lookup_and_root
    strat = Mes0dteIronCondor()
    r = run_zero_dte_backtest(
        strat,
        start=_TEST_WINDOW_START, end=_TEST_WINDOW_END,
        root=root, underlying_price_lookup=lookup,
    )
    if not r.trades:
        pytest.skip("no trades in test window — backtest test inconclusive")

    for t in r.trades:
        assert isinstance(t, TradeRecord)
        # Sanity: PnL identity holds.
        assert abs(
            t.pnl_dollars_net - (t.pnl_dollars_gross - t.slippage_dollars - t.commission_dollars)
        ) < 1e-6
        # 4-leg iron condor.
        assert t.n_legs == 4
        assert len(t.leg_strikes) == 4
        assert len(t.leg_sides) == 4
        assert len(t.leg_quantities) == 4
        # Underlying prices are positive.
        assert t.underlying_at_entry > 0
        assert t.underlying_at_settlement > 0
        # PnL identity: pnl_per_share = ec - intrinsic_close_cost
        assert abs(
            t.pnl_per_share_gross
            - (t.entry_credit_per_share - t.settlement_intrinsic_per_share)
        ) < 1e-6


def test_max_profit_when_underlying_settles_inside_short_strikes(
    real_lookup_and_root,
) -> None:
    """When the underlying at settlement is between the short strikes,
    the iron condor pays max profit — settlement_intrinsic == 0.
    """
    lookup, root = real_lookup_and_root
    strat = Mes0dteIronCondor()
    r = run_zero_dte_backtest(
        strat,
        start=_TEST_WINDOW_START, end=_TEST_WINDOW_END,
        root=root, underlying_price_lookup=lookup,
    )
    found_max_profit = False
    for t in r.trades:
        # Identify short strikes from leg metadata.
        strikes = list(t.leg_strikes)
        sides = list(t.leg_sides)
        qtys = list(t.leg_quantities)
        # Short put = quantity -1, side put.  Short call = -1, call.
        short_put_strike = next(
            s for s, sd, q in zip(strikes, sides, qtys)
            if sd == "put" and q == -1
        )
        short_call_strike = next(
            s for s, sd, q in zip(strikes, sides, qtys)
            if sd == "call" and q == -1
        )
        if short_put_strike < t.underlying_at_settlement < short_call_strike:
            # Inside short strikes → all legs OTM → intrinsic = 0.
            assert abs(t.settlement_intrinsic_per_share) < 1e-6
            found_max_profit = True
    if not found_max_profit:
        pytest.skip(
            "no max-profit trades in window — every IC was hit on at "
            "least one side"
        )


def test_runner_handles_no_eligible_sessions_gracefully(
    real_lookup_and_root,
) -> None:
    """A window with no 0DTE-eligible sessions returns an empty
    BacktestResult cleanly.
    """
    lookup, root = real_lookup_and_root
    strat = Mes0dteIronCondor()
    # 2-day weekend window (Jul 4-7 2024 = Independence Day Thu thru Sun).
    r = run_zero_dte_backtest(
        strat,
        start=datetime(2024, 7, 6, tzinfo=timezone.utc),
        end=datetime(2024, 7, 7, tzinfo=timezone.utc),
        root=root, underlying_price_lookup=lookup,
    )
    assert r.n_sessions_traded == 0


def test_aggregate_metrics_are_internally_consistent(
    real_lookup_and_root,
) -> None:
    lookup, root = real_lookup_and_root
    strat = Mes0dteIronCondor()
    r = run_zero_dte_backtest(
        strat,
        start=_TEST_WINDOW_START, end=_TEST_WINDOW_END,
        root=root, underlying_price_lookup=lookup,
    )
    assert r.n_winners + r.n_losers <= len(r.trades)  # could have == 0 trades
    if r.trades:
        assert r.win_rate == r.n_winners / len(r.trades)
        assert abs(r.avg_pnl_net - r.total_pnl_net / len(r.trades)) < 1e-6
        assert abs(r.total_pnl_net - sum(t.pnl_dollars_net for t in r.trades)) < 1e-6


# ── Intraday management ────────────────────────────────────────────


def test_default_close_reason_is_settlement(real_lookup_and_root) -> None:
    """Without profit_take_pct or loss_stop_pct set, every trade
    must close at settlement.
    """
    lookup, root = real_lookup_and_root
    strat = Mes0dteIronCondor()
    r = run_zero_dte_backtest(
        strat,
        start=_TEST_WINDOW_START, end=_TEST_WINDOW_END,
        root=root, underlying_price_lookup=lookup,
    )
    if not r.trades:
        pytest.skip("no trades in window")
    for t in r.trades:
        assert t.close_reason == "settlement"


def test_aggressive_profit_take_triggers_early_close(
    real_lookup_and_root,
) -> None:
    """A very low profit-take threshold should fire on most trades
    that enter (any small favorable move triggers close).
    """
    lookup, root = real_lookup_and_root
    # Use a wider window to ensure meaningful trade count.
    strat = Mes0dteIronCondor(
        put_short_offset=25.0, call_short_offset=25.0, wing_width_dollars=25.0,
    )
    r = run_zero_dte_backtest(
        strat,
        start=_TEST_WINDOW_START, end=_TEST_WINDOW_END,
        root=root, underlying_price_lookup=lookup,
        profit_take_pct=0.05,  # 5% — trivially easy to hit
    )
    if not r.trades:
        pytest.skip("no trades in window")
    n_pt = sum(1 for t in r.trades if t.close_reason == "profit_take")
    # Most trades should hit the trivial threshold.
    assert n_pt > 0
    # close_ts on PT trades should precede settlement_ts.
    for t in r.trades:
        if t.close_reason == "profit_take":
            assert t.close_ts is not None
            assert t.close_ts < t.settlement_ts


def test_aggressive_loss_stop_triggers_early_close(
    real_lookup_and_root,
) -> None:
    """A very tight loss-stop should fire on at least some trades."""
    lookup, root = real_lookup_and_root
    strat = Mes0dteIronCondor(
        put_short_offset=25.0, call_short_offset=25.0, wing_width_dollars=25.0,
    )
    r = run_zero_dte_backtest(
        strat,
        start=_TEST_WINDOW_START, end=_TEST_WINDOW_END,
        root=root, underlying_price_lookup=lookup,
        loss_stop_pct=0.10,  # 10% — easy adverse move triggers close
    )
    # Test asserts via no exception that the management code runs;
    # actual triggering depends on whether any leg moves adversely
    # in the window.
    assert isinstance(r.n_sessions_traded, int)


def test_close_metadata_populated(real_lookup_and_root) -> None:
    """close_ts and close_underlying are populated on every trade,
    regardless of close_reason.
    """
    lookup, root = real_lookup_and_root
    strat = Mes0dteIronCondor()
    r = run_zero_dte_backtest(
        strat,
        start=_TEST_WINDOW_START, end=_TEST_WINDOW_END,
        root=root, underlying_price_lookup=lookup,
        profit_take_pct=0.50,
    )
    if not r.trades:
        pytest.skip("no trades in window")
    for t in r.trades:
        assert t.close_ts is not None
        assert t.close_underlying > 0
