"""P&L invariant tests against real ORATS data.

Bug-fix regression coverage for the close-P&L sign inversion
discovered 2026-05-03 on the first full-year backtest:

  - The first iron-condor backtest on 250 days of real SPX data
    showed 100% hit rate / $163K realized P&L / max drawdown $0.
  - Inspection: trade 5 fired loss_stop at -238% of credit but
    recorded a POSITIVE $22,590 closed P&L.
  - Root cause: _close_position had `closed_credit = -close_per_
    share` then `pnl = entry_credit - closed_credit` which is
    `pnl = entry_credit + close_cost` — opposite sign of actual.
  - Fix: pnl = entry_credit - close_cost (matches mark_to_market
    formula exactly).

Invariant: realized P&L at close MUST equal mark_to_market at the
SAME snap (modulo commission). If they diverge, the close formula
is wrong.

Synthetic tests can't catch this because they don't run a full
backtest with management triggers + closes — only real-data multi-
day backtests exercise the closing path.
"""
from __future__ import annotations

import pytest

from tradegy.options.cost_model import OptionCostModel
from tradegy.options.runner import (
    _close_position,
    _open_position_from_order,
    run_options_backtest,
)
from tradegy.options.strategies import IronCondor45dteD16
from tradegy.options.strategy import ManagementRules


def test_close_pnl_has_same_sign_as_mark_to_market(real_spx_chain_snapshots):
    """Open an iron condor; mark it on a later snap; close it on
    the same snap. Close P&L and mark MUST have the same SIGN
    (both winners or both losers).

    They DO NOT need to be equal — mark_to_market uses chain mid;
    _close_position uses cost-model fill_price (mid ± spread
    offset). The expected difference per share is roughly
    `n_legs * half_spread * offset_fraction` unfavorable (we pay
    the spread on close); on a typical 4-leg SPX condor with
    OptionCostModel default that's ~$0.04-0.20 per share
    unfavorable.

    BUG REGRESSION: pre-fix, close gave +$13/share when mark gave
    -$3/share — opposite signs. The sign agreement assertion is
    the load-bearing check.
    """
    snap_entry = real_spx_chain_snapshots[0]
    snap_fill = real_spx_chain_snapshots[1]
    snap_close = real_spx_chain_snapshots[2]
    cost = OptionCostModel()

    order = IronCondor45dteD16().on_chain(snap_entry, ())
    pos = _open_position_from_order(
        order, snap_fill, cost, position_id="invariant",
    )
    assert pos is not None

    mark_per_share = pos.mark_to_market(snap_close)
    closed = _close_position(pos, snap_close, cost, reason="test")
    pnl_per_share_from_close = closed.closed_pnl_per_share

    # Sign agreement — the load-bearing regression check.
    assert (
        (pnl_per_share_from_close >= 0) == (mark_per_share >= 0)
    ), (
        f"close P&L per share {pnl_per_share_from_close} disagrees "
        f"in sign with mark_to_market {mark_per_share}; this is the "
        "close-P&L-sign-bug signature from 2026-05-03"
    )

    # Magnitude check: close should be WORSE than mark by at most
    # 4-leg-spread-offset (~$0.50/share generous bound on SPX).
    offset_drag = abs(pnl_per_share_from_close - mark_per_share)
    assert offset_drag < 0.50, (
        f"close vs mark divergence {offset_drag:.4f} exceeds "
        "4-leg cost-offset bound (~$0.50/share); investigate"
    )

    # When mark is negative (losing), close should be MORE negative
    # (we pay the spread to exit). When mark is positive, close
    # should be slightly LESS positive (same reason).
    assert pnl_per_share_from_close <= mark_per_share + 1e-9, (
        f"close P&L {pnl_per_share_from_close} is BETTER than mark "
        f"{mark_per_share} — closing the spread is supposed to cost "
        "money, not produce free P&L"
    )


def test_full_year_backtest_has_realistic_hit_rate_and_drawdown(
    real_spx_chain_snapshots,
):
    """Sanity check on the full-year run: vol-selling is a fat-
    left-tailed return distribution; a 250-day backtest of a
    45-DTE iron condor should have:
      - Some losing trades (tastytrade research shows 70-80% hit
        rate, NOT 100%).
      - A non-zero max drawdown (single losing trades wipe
        multiple winners).

    Test only runs when 30+ snapshots are available (we need a
    handful of complete trade lifecycles). Skips cleanly when
    only the small fixture is on disk.
    """
    if len(real_spx_chain_snapshots) < 30:
        pytest.skip(
            "needs 30+ real chain snapshots for management triggers "
            "to fire enough times; pull more data via "
            "download_spx_options_orats.py"
        )

    from tradegy.options.risk import RiskConfig, RiskManager
    strat = IronCondor45dteD16()
    risk = RiskManager(RiskConfig(
        declared_capital=250_000.0,
        max_capital_at_risk_pct=0.50,
        max_per_expiration_pct=0.50,
    ))
    result = run_options_backtest(
        strategy=strat, snapshots=real_spx_chain_snapshots, risk=risk,
    )
    assert result.n_closed_trades >= 5, (
        f"expected ≥5 closed trades on the full-year window; got "
        f"{result.n_closed_trades}"
    )
    # Hit rate < 100% — some trades MUST lose. Vol-selling fat-
    # left-tailed; 100% wins is the bug signature.
    assert result.hit_rate < 1.0, (
        f"100% hit rate is the close-P&L-sign-bug signature; got "
        f"{result.hit_rate * 100:.1f}%"
    )
    # Max drawdown is negative (some realized losses). Zero
    # drawdown on a long backtest of vol selling is the bug
    # signature too.
    assert result.max_drawdown_dollars < 0, (
        f"max drawdown should be negative on a 250-day vol-selling "
        f"backtest; got {result.max_drawdown_dollars:.2f} (the "
        "close-P&L-sign bug produced max_drawdown=0)"
    )
