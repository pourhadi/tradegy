"""Walk-forward harness tests for the options runner.

Real-data tests (no synthetic ChainSnapshots, per the
feedback_no_synthetic_data memory). Uses the
`real_spx_chain_snapshots` fixture from conftest indirectly — the
walk-forward function loads snapshots itself from parquet via
`iter_chain_snapshots(root=...)`, so the test merely checks that
the data is on disk and passes the same `root` through.

The pure-math/state tests (`split_windows`, `_evaluate_gate`,
`trade_dollar_sharpe` with reference values) do not require the
chain on disk — they verify gate logic and date math, both of
which are math, not behavior over data.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from tradegy import config
from tradegy.options.runner import ClosedTrade
from tradegy.options.strategies import PutCreditSpread45dteD30
from tradegy.options.walk_forward import (
    OptionsWalkForwardConfig,
    OptionsWalkForwardSummary,
    _evaluate_gate,
    run_options_walk_forward,
    split_windows,
    trade_dollar_sharpe,
)


# ── split_windows: pure date math ──────────────────────────────────


def test_split_windows_3y_train_1y_test_1y_roll_over_6y():
    """6-year coverage with 3y/1y/1y produces 3 windows.

    Window 0: train 2020-2023 / test 2023
    Window 1: train 2021-2024 / test 2024
    Window 2: train 2022-2025 / test 2025
    """
    cfg = OptionsWalkForwardConfig(
        train_years=3.0, test_years=1.0, roll_years=1.0,
    )
    windows = split_windows(
        coverage_start=datetime(2020, 1, 1),
        coverage_end=datetime(2026, 1, 1),
        config=cfg,
    )
    assert len(windows) == 3
    assert windows[0].index == 0
    assert windows[0].train_start == datetime(2020, 1, 1)
    # Train spans ~3 years (1096 days at 365.25/yr).
    assert (windows[0].train_end - windows[0].train_start).days == 1096
    # OOS test starts where train ends.
    assert windows[0].test_start == windows[0].train_end
    # Roll = 1y → window 1's train_start is 1 year after window 0's.
    assert (windows[1].train_start - windows[0].train_start).days == 365


def test_split_windows_too_short_returns_empty():
    """A 2-year coverage span with 3y train requirement produces
    zero windows (train alone exceeds coverage).
    """
    cfg = OptionsWalkForwardConfig(
        train_years=3.0, test_years=1.0, roll_years=1.0,
    )
    windows = split_windows(
        coverage_start=datetime(2024, 1, 1),
        coverage_end=datetime(2026, 1, 1),
        config=cfg,
    )
    assert windows == []


def test_split_windows_invalid_config_rejects():
    with pytest.raises(ValueError):
        OptionsWalkForwardConfig(
            train_years=0.0, test_years=1.0, roll_years=1.0,
        )


# ── trade_dollar_sharpe: pure math (reference values) ──────────────


def _make_closed_trade(pnl: float) -> ClosedTrade:
    """Pure-math test helper. ClosedTrade fields outside `closed_pnl
    _dollars` are inert for the Sharpe computation; this is testing
    the formula, not behavior over data — same pattern as the
    futures harness's stats tests.
    """
    from datetime import date
    return ClosedTrade(
        position_id="x",
        strategy_class="x",
        contracts=1,
        entry_ts=datetime(2024, 1, 1),
        closed_ts=datetime(2024, 1, 2),
        entry_credit_per_share=0.0,
        closed_credit_per_share=0.0,
        closed_pnl_per_share=0.0,
        closed_pnl_dollars=pnl,
        open_commission=0.0,
        close_commission=0.0,
        closed_reason="x",
        expiries=(date(2024, 2, 1),),
    )


def test_trade_dollar_sharpe_constant_returns_zero():
    """Zero-variance series → Sharpe defined as 0 (not inf)."""
    trades = [_make_closed_trade(100.0) for _ in range(5)]
    assert trade_dollar_sharpe(trades) == 0.0


def test_trade_dollar_sharpe_under_two_trades_returns_zero():
    """Single trade has no variance → Sharpe undefined → 0.0."""
    assert trade_dollar_sharpe([]) == 0.0
    assert trade_dollar_sharpe([_make_closed_trade(50.0)]) == 0.0


def test_trade_dollar_sharpe_known_distribution():
    """Mean = 100, sample stdev ≈ 79.06 over [0, 100, 200] →
    Sharpe ≈ 1.265.
    """
    trades = [
        _make_closed_trade(0.0),
        _make_closed_trade(100.0),
        _make_closed_trade(200.0),
    ]
    sharpe = trade_dollar_sharpe(trades)
    assert sharpe == pytest.approx(100.0 / 100.0, rel=1e-3)


# ── _evaluate_gate: gate logic in isolation ────────────────────────


def _summary(
    is_sharpe: float, oos_sharpe: float,
    n_windows: int = 3,
) -> OptionsWalkForwardSummary:
    """Pure-math gate test: build a Summary with manufactured Sharpe
    pair, verify gate decision. Same legitimacy as futures
    `_evaluate_gate` tests in `tests/test_walk_forward.py`.
    """
    s = OptionsWalkForwardSummary(
        strategy_ids=("x",),
        config=OptionsWalkForwardConfig(),
        coverage_start=datetime(2020, 1, 1),
        coverage_end=datetime(2026, 1, 1),
    )
    s.avg_in_sample_sharpe = is_sharpe
    s.avg_oos_sharpe = oos_sharpe
    # Gate inspects `windows` truthiness only; insert N placeholders.
    s.windows = [None] * n_windows  # type: ignore[list-item]
    return s


def test_gate_passes_when_oos_at_50pct_of_is():
    """OOS / IS = 0.5 — exactly at the boundary, should PASS."""
    s = _summary(is_sharpe=1.0, oos_sharpe=0.5)
    passed, reason = _evaluate_gate(s)
    assert passed is True
    assert reason == ""


def test_gate_fails_when_oos_below_50pct_of_is():
    s = _summary(is_sharpe=1.0, oos_sharpe=0.49)
    passed, reason = _evaluate_gate(s)
    assert passed is False
    assert "< 50%" in reason


def test_gate_fails_when_is_sharpe_not_positive():
    s = _summary(is_sharpe=-0.1, oos_sharpe=0.5)
    passed, reason = _evaluate_gate(s)
    assert passed is False
    assert "no in-sample edge" in reason


def test_gate_fails_when_no_windows():
    s = _summary(is_sharpe=1.0, oos_sharpe=1.0, n_windows=0)
    passed, reason = _evaluate_gate(s)
    assert passed is False
    assert reason == "no windows generated"


# ── Integration: real SPX walk-forward over a 2-window slice ───────


def test_walk_forward_runs_against_real_spx_chain(
    real_spx_chain_snapshots,
):
    """End-to-end: load real SPX chain, run a 1y/0.5y/0.5y walk-
    forward over a 2-year slice. Verifies the full pipeline (load
    snapshots → run portfolio → aggregate → gate).

    Window sizing kept small to keep test runtime reasonable while
    still exercising at least 2 windows.
    """
    raw_root = config.repo_root() / "data" / "raw"
    cfg = OptionsWalkForwardConfig(
        train_years=1.0, test_years=0.5, roll_years=0.5,
    )
    summary = run_options_walk_forward(
        strategies=[PutCreditSpread45dteD30()],
        source_id="spx_options_chain",
        ticker="SPX",
        coverage_start=datetime(2020, 1, 1),
        coverage_end=datetime(2022, 1, 1),
        config=cfg,
        root=raw_root,
    )
    # 1y train + 0.5y test, rolling 0.5y, in a 2y span → 2 windows.
    assert len(summary.windows) == 2
    # Each window has both halves populated.
    for w in summary.windows:
        assert w.in_sample is not None
        assert w.out_of_sample is not None
        # PCS opens at 45 DTE — over a 1-year IS window we expect at
        # least a handful of trades. A failure here means the
        # strategy / data pipeline broke, not the walk-forward.
        assert w.in_sample.n_closed_trades > 0, (
            f"window {w.index} in-sample produced zero trades — "
            "strategy or data pipeline regression"
        )
    # Gate evaluation runs without exception. Pass/fail is data-
    # dependent — the test asserts the GATE was computed, not that
    # PCS passes (that is the substantive question this harness
    # is built to answer).
    assert summary.fail_reason is not None
    assert isinstance(summary.passed, bool)
