"""Real-data tests for Tier 3: SkewGated + TermStructureGated
wrappers + ReverseIronCondor + CallDiagonal.

Per the no-synthetic-data rule.
"""
from __future__ import annotations

import pytest

from tradegy.options.chain import OptionSide
from tradegy.options.cost_model import OptionCostModel
from tradegy.options.risk import RiskManager, RiskConfig, compute_portfolio_greeks
from tradegy.options.runner import _open_position_from_order, run_options_backtest
from tradegy.options.strategies import (
    CallDiagonal30_60,
    IronCondor45dteD16,
    PutCreditSpread45dteD30,
    ReverseIronCondor45dteD30,
    SkewGatedStrategy,
    TermStructureGatedStrategy,
)


# ── SkewGatedStrategy ──────────────────────────────────────────


def test_skew_gated_id_derivation(real_spx_chain_snapshots):
    base = PutCreditSpread45dteD30()
    g = SkewGatedStrategy(base=base, min_skew_rank=0.5)
    assert "skew_gated" in g.id
    assert "min0.50" in g.id
    assert "put_credit_spread" in g.id


def test_skew_gated_warmup_returns_none(real_spx_chain_snapshots):
    """Before min_history_days reached, wrapper returns None."""
    g = SkewGatedStrategy(
        base=PutCreditSpread45dteD30(),
        min_skew_rank=0.5,
        window_days=60, min_history_days=60,
    )
    for s in real_spx_chain_snapshots[:30]:
        assert g.on_chain(s, ()) is None


def test_skew_gated_min_skew_restricts_entries(real_spx_chain_snapshots):
    """min_skew_rank=0.99 should drastically reduce entries vs base."""
    risk = RiskManager(RiskConfig(declared_capital=250_000.0))
    base_result = run_options_backtest(
        strategy=PutCreditSpread45dteD30(),
        snapshots=real_spx_chain_snapshots, risk=risk,
    )
    gated_result = run_options_backtest(
        strategy=SkewGatedStrategy(
            base=PutCreditSpread45dteD30(),
            min_skew_rank=0.99,
            window_days=60, min_history_days=60,
        ),
        snapshots=real_spx_chain_snapshots, risk=risk,
    )
    assert gated_result.n_closed_trades < base_result.n_closed_trades


# ── TermStructureGatedStrategy ─────────────────────────────────


def test_ts_gated_id_derivation(real_spx_chain_snapshots):
    g = TermStructureGatedStrategy(
        base=PutCreditSpread45dteD30(), max_slope=0.0,
    )
    assert "ts_gated" in g.id
    assert "put_credit_spread" in g.id


def test_ts_gated_max_slope_restricts_entries(real_spx_chain_snapshots):
    """max_slope=-0.10 (deep contango required) should restrict
    entries vs the base; not all days are in deep contango.
    """
    risk = RiskManager(RiskConfig(declared_capital=250_000.0))
    base_result = run_options_backtest(
        strategy=PutCreditSpread45dteD30(),
        snapshots=real_spx_chain_snapshots, risk=risk,
    )
    gated_result = run_options_backtest(
        strategy=TermStructureGatedStrategy(
            base=PutCreditSpread45dteD30(),
            max_slope=-0.10,
        ),
        snapshots=real_spx_chain_snapshots, risk=risk,
    )
    assert gated_result.n_closed_trades < base_result.n_closed_trades


def test_ts_gated_no_filter_when_neither_set(real_spx_chain_snapshots):
    """Both min/max=None → wrapper applies no filter; behavior
    matches the bare base strategy."""
    g = TermStructureGatedStrategy(base=PutCreditSpread45dteD30())
    base = PutCreditSpread45dteD30()
    snap = real_spx_chain_snapshots[0]
    base_order = base.on_chain(snap, ())
    gated_order = g.on_chain(snap, ())
    if base_order is None:
        assert gated_order is None
    else:
        assert gated_order is not None
        # Same legs (no filter applied).
        assert sorted((l.strike, l.quantity, l.side.value) for l in base_order.legs) == \
               sorted((l.strike, l.quantity, l.side.value) for l in gated_order.legs)


# ── ReverseIronCondor ──────────────────────────────────────────


def test_ric_4leg_inverted_signs(real_spx_chain_snapshots):
    """RIC has the SIGNS of an iron condor INVERTED:
    long body (the IC's short), short wings (the IC's long)."""
    snap = real_spx_chain_snapshots[0]
    order = ReverseIronCondor45dteD30().on_chain(snap, ())
    assert order is not None
    assert order.tag == "reverse_iron_condor_45dte_d30"
    assert len(order.legs) == 4
    sides = sorted(l.side.value for l in order.legs)
    assert sides == ["call", "call", "put", "put"]


def test_ric_is_debit_position(real_spx_chain_snapshots):
    """RIC pays premium (debit). entry_credit_per_share < 0."""
    snap_entry = real_spx_chain_snapshots[0]
    snap_fill = real_spx_chain_snapshots[1]
    order = ReverseIronCondor45dteD30().on_chain(snap_entry, ())
    pos = _open_position_from_order(
        order, snap_fill, OptionCostModel(), position_id="ric",
    )
    assert pos is not None
    assert pos.entry_credit_per_share < 0, (
        f"RIC should be net debit; got "
        f"{pos.entry_credit_per_share:.2f}"
    )


def test_ric_long_body_inside_short_wings(real_spx_chain_snapshots):
    """Long body legs sit BETWEEN the short wing legs (the body
    captures movement toward ATM; wings cap profit if the move is
    too big).
    """
    snap = real_spx_chain_snapshots[0]
    order = ReverseIronCondor45dteD30().on_chain(snap, ())
    long_call = next(l for l in order.legs if l.side == OptionSide.CALL and l.quantity == +1)
    short_call = next(l for l in order.legs if l.side == OptionSide.CALL and l.quantity == -1)
    long_put = next(l for l in order.legs if l.side == OptionSide.PUT and l.quantity == +1)
    short_put = next(l for l in order.legs if l.side == OptionSide.PUT and l.quantity == -1)
    # Long call (body) is BELOW short call (wing)
    assert long_call.strike < short_call.strike
    # Long put (body) is ABOVE short put (wing)
    assert long_put.strike > short_put.strike


def test_ric_portfolio_signs_long_vol(real_spx_chain_snapshots):
    """RIC is LONG vol: vega positive (benefits from IV expansion).
    Theta NEGATIVE (we lose to time decay — pay for the optionality).
    """
    snap_entry = real_spx_chain_snapshots[0]
    snap_fill = real_spx_chain_snapshots[1]
    mark_index = min(5, len(real_spx_chain_snapshots) - 1)
    snap_mark = real_spx_chain_snapshots[mark_index]
    order = ReverseIronCondor45dteD30().on_chain(snap_entry, ())
    pos = _open_position_from_order(
        order, snap_fill, OptionCostModel(), position_id="ric_g",
    )
    g = compute_portfolio_greeks([pos], snap_mark)
    assert g.vega_dollars > 0, (
        f"RIC should be net long vol → vega POSITIVE; got {g.vega_dollars}"
    )
    assert g.theta_dollars < 0, (
        f"RIC pays for optionality → theta NEGATIVE; got {g.theta_dollars}"
    )


# ── CallDiagonal ───────────────────────────────────────────────


def test_call_diagonal_2leg_diff_strikes_diff_expiries(
    real_spx_chain_snapshots,
):
    snap = real_spx_chain_snapshots[0]
    order = CallDiagonal30_60().on_chain(snap, ())
    assert order is not None
    assert order.tag == "call_diagonal_30_60_d30_d10"
    assert len(order.legs) == 2
    assert all(l.side == OptionSide.CALL for l in order.legs)
    short = next(l for l in order.legs if l.quantity == -1)
    long = next(l for l in order.legs if l.quantity == +1)
    assert short.expiry != long.expiry
    assert short.strike != long.strike


def test_call_diagonal_long_above_short(real_spx_chain_snapshots):
    """Bearish bias: long protective leg sits ABOVE short body."""
    snap = real_spx_chain_snapshots[0]
    order = CallDiagonal30_60().on_chain(snap, ())
    short = next(l for l in order.legs if l.quantity == -1)
    long = next(l for l in order.legs if l.quantity == +1)
    assert long.strike > short.strike


def test_call_diagonal_portfolio_delta_negative(real_spx_chain_snapshots):
    """Bearish-bias diagonal → net negative delta (we benefit from
    underlying staying flat or going down).
    """
    snap_entry = real_spx_chain_snapshots[0]
    snap_fill = real_spx_chain_snapshots[1]
    mark_index = min(5, len(real_spx_chain_snapshots) - 1)
    snap_mark = real_spx_chain_snapshots[mark_index]
    order = CallDiagonal30_60().on_chain(snap_entry, ())
    pos = _open_position_from_order(
        order, snap_fill, OptionCostModel(), position_id="cd",
    )
    g = compute_portfolio_greeks([pos], snap_mark)
    assert g.delta_dollars < 0
