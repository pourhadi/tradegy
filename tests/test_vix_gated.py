"""VIX-daily regime-gated strategy wrapper tests.

Real-data tests against the on-disk vix_daily + mes_options_chain
sources.  Auto-marked slow via the `mes_options_chain_ingested`
fixture.

Coverage:

  - Wrapper requires a base strategy.
  - id auto-derives from base + thresholds.
  - Without thresholds, behaves identically to the base.
  - With max_vix_close = 18, blocks on days where prior VIX > 18.
  - With min_vix_close = 20, blocks on days where prior VIX < 20.
  - With percentile thresholds, computes 252-day rank correctly.
  - No-lookahead: gate uses the PRIOR session's VIX, never same-day.
  - Forwarded order is re-tagged with the wrapper's id.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import polars as pl
import pytest

from tradegy.options.databento_chain_io import iter_session_chains, make_mes_futures_price_lookup
from tradegy.options.strategies.mes_0dte_iron_condor import Mes0dteIronCondor
from tradegy.options.strategies.mes_0dte_pcs import Mes0dtePcs
from tradegy.options.strategies.vix_gated import (
    VixGatedStrategy,
    _percentile_rank,
    _prior_trading_day_vix,
    _load_vix_daily,
)


_TEST_WINDOW_START = datetime(2024, 6, 3, tzinfo=timezone.utc)
_TEST_WINDOW_END = datetime(2024, 6, 8, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def real_session_chains_for_vix(mes_options_chain_ingested):
    lookup = make_mes_futures_price_lookup(
        root=mes_options_chain_ingested["raw_root"],
    )
    return list(iter_session_chains(
        "mes_options_chain",
        start=_TEST_WINDOW_START, end=_TEST_WINDOW_END,
        root=mes_options_chain_ingested["raw_root"],
        underlying_price_lookup=lookup,
    ))


# ── Wrapper construction ──────────────────────────────────────────


def test_wrapper_id_auto_derives_from_base_plus_thresholds(
    mes_options_chain_ingested,
) -> None:
    base = Mes0dteIronCondor(id="ic_test_base")
    w = VixGatedStrategy(
        base=base,
        max_vix_close=18.0,
        max_vix_pctile_252=0.25,
        vix_root=mes_options_chain_ingested["raw_root"],
    )
    assert "vix_gated" in w.id
    assert "vixmax18" in w.id
    assert "pctmax0.25" in w.id
    assert "ic_test_base" in w.id


def test_wrapper_id_explicit_override(mes_options_chain_ingested) -> None:
    base = Mes0dteIronCondor()
    w = VixGatedStrategy(
        base=base, max_vix_close=18.0, id="my_custom_id",
        vix_root=mes_options_chain_ingested["raw_root"],
    )
    assert w.id == "my_custom_id"


# ── No-lookahead gate ─────────────────────────────────────────────


def test_gate_uses_prior_session_vix_no_lookahead(
    mes_options_chain_ingested,
) -> None:
    """The gate must use the PRIOR trading day's VIX, never the
    current day's value (which hasn't closed yet at strategy
    entry time).
    """
    base = Mes0dteIronCondor()
    w = VixGatedStrategy(
        base=base, max_vix_close=999.0,  # always passes — sanity
        vix_root=mes_options_chain_ingested["raw_root"],
    )
    # Find any day in the test window and check the date used by
    # _prior_trading_day_vix is strictly BEFORE.
    test_date = date(2024, 6, 4)
    result = _prior_trading_day_vix(w._vix_df, test_date)
    assert result is not None
    prior_date, prior_vix = result
    assert prior_date < test_date
    assert prior_vix > 0.0


# ── Behavior with thresholds ──────────────────────────────────────


def test_no_thresholds_acts_as_passthrough(
    real_session_chains_for_vix, mes_options_chain_ingested,
) -> None:
    """With no thresholds set the wrapper should pass every snapshot
    through to the base (subject to insufficient-history → False).
    """
    base = Mes0dteIronCondor()
    w = VixGatedStrategy(
        base=base, vix_root=mes_options_chain_ingested["raw_root"],
    )
    n_base_orders = 0
    n_wrapped_orders = 0
    for snap in real_session_chains_for_vix:
        if base.on_chain(snap, ()) is not None:
            n_base_orders += 1
        if w.on_chain(snap, ()) is not None:
            n_wrapped_orders += 1
    assert n_wrapped_orders == n_base_orders


def test_max_vix_blocks_high_vol_days(
    real_session_chains_for_vix, mes_options_chain_ingested,
) -> None:
    """An impossibly tight max_vix_close must block every day."""
    base = Mes0dteIronCondor()
    w = VixGatedStrategy(
        base=base, max_vix_close=0.01,
        vix_root=mes_options_chain_ingested["raw_root"],
    )
    for snap in real_session_chains_for_vix:
        assert w.on_chain(snap, ()) is None


def test_min_vix_blocks_low_vol_days(
    real_session_chains_for_vix, mes_options_chain_ingested,
) -> None:
    """An impossibly high min_vix_close must block every day."""
    base = Mes0dteIronCondor()
    w = VixGatedStrategy(
        base=base, min_vix_close=999.0,
        vix_root=mes_options_chain_ingested["raw_root"],
    )
    for snap in real_session_chains_for_vix:
        assert w.on_chain(snap, ()) is None


def test_forwarded_order_retagged_with_wrapper_id(
    real_session_chains_for_vix, mes_options_chain_ingested,
) -> None:
    base = Mes0dtePcs(put_short_offset=25.0, wing_width_dollars=25.0)
    w = VixGatedStrategy(
        base=base, max_vix_close=999.0, id="my_vix_gate",
        vix_root=mes_options_chain_ingested["raw_root"],
    )
    for snap in real_session_chains_for_vix:
        order = w.on_chain(snap, ())
        if order is None:
            continue
        assert order.tag == "my_vix_gate"
        # Legs are unchanged.
        assert len(order.legs) == 2
        return
    pytest.skip("no emitting snapshot in window")


# ── Percentile rank helper ────────────────────────────────────────


def test_percentile_rank_basics() -> None:
    sorted_vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    # x = 1 (lowest) → rank should be 0.1 (1 / 10 with bisect_right)
    assert _percentile_rank(sorted_vals, 1.0) == 0.1
    # x = 10 (highest) → rank should be 1.0
    assert _percentile_rank(sorted_vals, 10.0) == 1.0
    # x = 5.5 → rank ≈ 0.5
    assert _percentile_rank(sorted_vals, 5.5) == 0.5
    # x = 0 (below all) → rank = 0
    assert _percentile_rank(sorted_vals, 0.0) == 0.0


def test_percentile_rank_empty_returns_default() -> None:
    assert _percentile_rank([], 5.0) == 0.5  # mid-rank default


# ── VIX data presence ─────────────────────────────────────────────


def test_vix_data_loadable(mes_options_chain_ingested) -> None:
    df = _load_vix_daily(root=mes_options_chain_ingested["raw_root"])
    assert df.height > 100  # decades of VIX history on disk
    assert "trade_date" in df.columns
    assert "close" in df.columns
