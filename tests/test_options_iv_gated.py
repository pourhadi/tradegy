"""IV-gated strategy wrapper tests against real ORATS data.

Per the no-synthetic-data rule. Verifies the wrapper correctly:
  - Inherits the base strategy's order shape when the gate passes.
  - Returns None when current ATM IV rank is below min_iv_rank.
  - Returns None when current ATM IV rank is above max_iv_rank.
  - Returns None during the warmup window (insufficient history).
  - Composes with the runner so backtests work end-to-end.
  - Derives a meaningful audit-trail id.

The 250-day fixture has just enough data for a window_days=60
test (rank meaningful from day 60 onward) but NOT enough for
window_days=252 (we'd see ranks only on the very last snap, if
at all). Tests use window_days=60 so the wrapper has enough
history to be informative.
"""
from __future__ import annotations

import pytest

from tradegy.options.runner import run_options_backtest
from tradegy.options.risk import RiskManager, RiskConfig
from tradegy.options.strategies import (
    IronCondor45dteD16,
    IvGatedStrategy,
    PutCreditSpread45dteD30,
)


def test_id_derived_from_base_and_gate_params(real_spx_chain_snapshots):
    base = PutCreditSpread45dteD30()
    g = IvGatedStrategy(base=base, min_iv_rank=0.5, max_iv_rank=0.95)
    assert "iv_gated" in g.id
    assert "min0.50" in g.id
    assert "max0.95" in g.id
    assert "put_credit_spread" in g.id


def test_unset_gates_default_to_no_filtering(real_spx_chain_snapshots):
    """Both min/max=None → wrapper still requires warmup but
    applies no rank filter. After warmup, behavior is identical
    to the wrapped base.
    """
    base = PutCreditSpread45dteD30()
    g = IvGatedStrategy(base=base, window_days=60, min_history_days=60)
    # Walk through the first ~70 snaps; verify no gate rejection
    # AFTER warmup. Use snapshots[60+] as "post-warmup."
    post_warmup = real_spx_chain_snapshots[65]
    # First feed the wrapper with 65 snaps so its history is full.
    for s in real_spx_chain_snapshots[:65]:
        g.on_chain(s, ())
    # Now ask for an order on a post-warmup snap.
    order = g.on_chain(post_warmup, ())
    # Behavior should match the bare base on the same input
    # (no rank filter applied, history sufficient).
    base_order = base.on_chain(post_warmup, ())
    if order is None:
        assert base_order is None
    else:
        assert base_order is not None
        # Same legs (same strikes, same sides) since rank filter
        # is OFF.
        assert sorted((l.strike, l.quantity, l.side.value) for l in order.legs) == \
               sorted((l.strike, l.quantity, l.side.value) for l in base_order.legs)


def test_warmup_returns_none(real_spx_chain_snapshots):
    """Before min_history_days is reached, the wrapper returns None
    regardless of rank or base strategy decision.
    """
    base = PutCreditSpread45dteD30()
    g = IvGatedStrategy(base=base, window_days=60, min_history_days=60)
    # Feed first 30 snaps — well below the 60-day floor.
    for s in real_spx_chain_snapshots[:30]:
        order = g.on_chain(s, ())
        assert order is None, (
            f"wrapper should return None during warmup; got order at "
            f"snap {s.ts_utc}"
        )


def test_min_rank_blocks_low_iv_entries(real_spx_chain_snapshots):
    """Setting min_iv_rank=0.99 forces the wrapper to skip almost
    every snap (only the absolute IV-rank-leading day passes).
    On the full-year fixture this should produce drastically
    fewer entries than the bare base.
    """
    risk = RiskManager(RiskConfig(declared_capital=250_000.0))

    base_result = run_options_backtest(
        strategy=PutCreditSpread45dteD30(),
        snapshots=real_spx_chain_snapshots,
        risk=risk,
    )
    gated_result = run_options_backtest(
        strategy=IvGatedStrategy(
            base=PutCreditSpread45dteD30(),
            min_iv_rank=0.99,  # only top ~1% of days qualify
            window_days=60,
            min_history_days=60,
        ),
        snapshots=real_spx_chain_snapshots,
        risk=risk,
    )
    # Gated should have STRICTLY FEWER closed trades than base
    # (almost-no-entries gate).
    assert gated_result.n_closed_trades < base_result.n_closed_trades, (
        f"min_iv_rank=0.99 should drastically restrict entries; "
        f"got base={base_result.n_closed_trades}, "
        f"gated={gated_result.n_closed_trades}"
    )


def test_max_rank_blocks_high_iv_entries(real_spx_chain_snapshots):
    """max_iv_rank=0.01 makes virtually no day pass (only the
    absolute lowest IV in the trailing window qualifies).
    """
    risk = RiskManager(RiskConfig(declared_capital=250_000.0))
    base_result = run_options_backtest(
        strategy=PutCreditSpread45dteD30(),
        snapshots=real_spx_chain_snapshots,
        risk=risk,
    )
    gated_result = run_options_backtest(
        strategy=IvGatedStrategy(
            base=PutCreditSpread45dteD30(),
            max_iv_rank=0.01,
            window_days=60,
            min_history_days=60,
        ),
        snapshots=real_spx_chain_snapshots,
        risk=risk,
    )
    assert gated_result.n_closed_trades < base_result.n_closed_trades


def test_runner_integration_records_gated_strategy_id(
    real_spx_chain_snapshots,
):
    """Backtest result.strategy_id reflects the wrapper's id, not
    the base's. So an audit-trail consumer can distinguish raw
    PCS from IV-gated PCS.
    """
    risk = RiskManager(RiskConfig(declared_capital=250_000.0))
    g = IvGatedStrategy(
        base=PutCreditSpread45dteD30(),
        min_iv_rank=0.5,
        window_days=60,
        min_history_days=60,
    )
    result = run_options_backtest(
        strategy=g, snapshots=real_spx_chain_snapshots, risk=risk,
    )
    assert "iv_gated" in result.strategy_id
    assert "put_credit_spread" in result.strategy_id


def test_iv_gated_wraps_iron_condor_too(real_spx_chain_snapshots):
    """The wrapper is base-agnostic — should compose with any
    OptionStrategy."""
    risk = RiskManager(RiskConfig(declared_capital=250_000.0))
    g = IvGatedStrategy(
        base=IronCondor45dteD16(),
        min_iv_rank=0.5,
        window_days=60,
        min_history_days=60,
    )
    result = run_options_backtest(
        strategy=g, snapshots=real_spx_chain_snapshots, risk=risk,
    )
    assert "iron_condor" in result.strategy_id
