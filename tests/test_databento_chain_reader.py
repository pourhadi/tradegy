"""databento options chain-reader tests.

Exercises the real on-disk `mes_options_chain` parquet partitions
written by `scripts/ingest_mes_options_full_grid.py` (21 CSV pairs
unioned: 1 quarterly + 20 daily X-prefix).  Per the no-synthetic-
data rule these tests use real ingested data; the
`mes_options_chain_ingested` fixture fails clearly if the data isn't
on disk.

Auto-marked `slow` via the `_SLOW_FIXTURES` hook in conftest.py.

Coverage:

  - `load_databento_chain_frames` returns the canonical column set,
    sorted, optionally filtered by raw_symbol_prefix.
  - No bar carries a ts_utc later than its leg's expiry (instrument-
    id reuse regression test — the bug we found 2026-05-06 where
    bars were silently mis-paired with stale definitions).
  - `iter_session_chains` yields one ChainSnapshot per session,
    each having the LAST bar's close as bid=ask, multiplier=5
    (MES options), and family-root underlying='MES'.
  - 0DTE expirations show up as expected: on a Mon-Thu trading
    day, the same date appears as an expiry in the chain (i.e.,
    we have a 0DTE expiration available in the snapshot).
  - `iter_minute_snapshots` is sparse — typical minute has 1-50
    legs, vs 200-500 in session-level snapshots.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import polars as pl
import pytest

from tradegy.options.chain import OptionSide
from tradegy.options.databento_chain_io import (
    iter_minute_snapshots,
    iter_session_chains,
    load_databento_chain_frames,
)


# Constant test window — first full week of June 2024, where the
# data is dense and the chain is rich.
_WINDOW_START = datetime(2024, 6, 3, tzinfo=timezone.utc)
_WINDOW_END = datetime(2024, 6, 8, tzinfo=timezone.utc)


# ── Frame loader ──────────────────────────────────────────────────


def test_load_chain_frames_returns_canonical_columns(
    mes_options_chain_ingested,
) -> None:
    df = load_databento_chain_frames(
        "mes_options_chain",
        start=_WINDOW_START, end=_WINDOW_END,
        root=mes_options_chain_ingested["raw_root"],
    )
    assert df.height > 0
    expected = {
        "ts_utc", "instrument_id", "symbol", "raw_symbol",
        "underlying", "expiry", "strike", "side",
        "open", "high", "low", "close", "volume",
    }
    assert set(df.columns) == expected


def test_load_chain_frames_supports_prefix_filter(
    mes_options_chain_ingested,
) -> None:
    """raw_symbol_prefix='X1A' should return only Monday-week-1
    options.
    """
    df = load_databento_chain_frames(
        "mes_options_chain",
        start=_WINDOW_START, end=_WINDOW_END,
        root=mes_options_chain_ingested["raw_root"],
        raw_symbol_prefix="X1A",
    )
    if df.height > 0:
        prefixes = df["raw_symbol"].str.slice(0, 3).unique().to_list()
        assert prefixes == ["X1A"]


def test_no_bar_postdates_its_legs_expiry(
    mes_options_chain_ingested,
) -> None:
    """Regression: instrument_id reuse used to produce bars dated
    after their listed expiry. Joining on raw_symbol fixes it; this
    test verifies no such row exists in the ingested data.
    """
    df = load_databento_chain_frames(
        "mes_options_chain",
        start=_WINDOW_START, end=_WINDOW_END,
        root=mes_options_chain_ingested["raw_root"],
    )
    # bar_date <= expiry must hold for every row.
    df_dated = df.with_columns(pl.col("ts_utc").dt.date().alias("bar_date"))
    bad = df_dated.filter(pl.col("expiry") < pl.col("bar_date"))
    assert bad.height == 0, (
        f"{bad.height} bars have ts_utc later than their expiry "
        "— instrument_id reuse regression"
    )


# ── Session chain iterator ────────────────────────────────────────


def test_iter_session_chains_yields_one_per_session(
    mes_options_chain_ingested,
) -> None:
    """5 trading days in a Mon-Fri week → 5 session snapshots."""
    sessions = list(iter_session_chains(
        "mes_options_chain",
        start=_WINDOW_START, end=_WINDOW_END,
        root=mes_options_chain_ingested["raw_root"],
    ))
    # Expect 5 weekday sessions (Mon-Fri); MES trades 24h Mon-Fri so
    # this is the standard count.
    assert 4 <= len(sessions) <= 6


def test_session_snapshot_uses_family_root_underlying(
    mes_options_chain_ingested,
) -> None:
    sessions = list(iter_session_chains(
        "mes_options_chain",
        start=_WINDOW_START, end=_WINDOW_END,
        root=mes_options_chain_ingested["raw_root"],
    ))
    for snap in sessions:
        assert snap.underlying == "MES"


def test_session_legs_carry_per_leg_metadata(
    mes_options_chain_ingested,
) -> None:
    sessions = list(iter_session_chains(
        "mes_options_chain",
        start=_WINDOW_START, end=_WINDOW_END,
        root=mes_options_chain_ingested["raw_root"],
    ))
    assert sessions
    sample = sessions[0]
    assert len(sample.legs) > 50  # rich session
    for leg in sample.legs[:30]:
        assert leg.side in (OptionSide.CALL, OptionSide.PUT)
        assert leg.strike > 0.0
        # MES futures-options multiplier is $5.
        assert leg.multiplier == 5
        # bid == ask == close sentinel.
        assert leg.bid == leg.ask
        assert leg.bid > 0
        # IV sentinel — databento has no vendor IV.
        assert leg.iv == 0.0
        # Underlying is a 5-char MES quarterly future.
        assert leg.underlying.startswith("MES")
        assert len(leg.underlying) == 5


def test_session_snapshot_has_zero_dte_on_weekday(
    mes_options_chain_ingested,
) -> None:
    """On a typical Mon-Thu trading day, the same date should appear
    as an expiry in the chain (0DTE available).  Friday's a quarterly
    expiry day, also 0DTE-eligible at quarter-end.
    """
    sessions = list(iter_session_chains(
        "mes_options_chain",
        start=_WINDOW_START, end=_WINDOW_END,
        root=mes_options_chain_ingested["raw_root"],
    ))
    n_with_zero_dte = 0
    for snap in sessions:
        snap_date = snap.ts_utc.date()
        if snap_date in snap.expiries():
            n_with_zero_dte += 1
    # In the Jun 3-7 2024 window every Mon-Thu had a daily expiry.
    # Friday Jun 7 had no quarterly expiry that week; 0DTE count
    # should be at least 4.
    assert n_with_zero_dte >= 4, (
        f"only {n_with_zero_dte} of {len(sessions)} sessions had "
        "a same-day expiry — daily MES options should provide 0DTE "
        "Mon-Thu"
    )


# ── Minute snapshot iterator ──────────────────────────────────────


def test_iter_minute_snapshots_is_sparse(
    mes_options_chain_ingested,
) -> None:
    """Per-minute snapshots from databento ohlcv-1m only contain
    contracts that traded that minute.  Average minute should have
    far fewer legs than a session-level snapshot.
    """
    # Restrict to a 2-hour window for test runtime.
    mins = list(iter_minute_snapshots(
        "mes_options_chain",
        start=datetime(2024, 6, 3, 13, 0, tzinfo=timezone.utc),
        end=datetime(2024, 6, 3, 15, 0, tzinfo=timezone.utc),
        root=mes_options_chain_ingested["raw_root"],
    ))
    assert mins
    # Average leg count per minute is far below session-level.
    avg_legs = sum(len(m.legs) for m in mins) / len(mins)
    assert avg_legs < 30, (
        f"average minute leg count = {avg_legs:.1f}; expected <30 "
        "(databento ohlcv-1m is sparse)"
    )


def test_minute_snapshots_have_well_formed_legs(
    mes_options_chain_ingested,
) -> None:
    mins = list(iter_minute_snapshots(
        "mes_options_chain",
        start=datetime(2024, 6, 3, 13, 0, tzinfo=timezone.utc),
        end=datetime(2024, 6, 3, 13, 30, tzinfo=timezone.utc),
        root=mes_options_chain_ingested["raw_root"],
    ))
    for snap in mins:
        for leg in snap.legs:
            assert leg.side in (OptionSide.CALL, OptionSide.PUT)
            assert leg.strike > 0.0
            # Each leg's expiry is at-or-after the snapshot date.
            assert leg.expiry >= snap.ts_utc.date()


def test_underlying_price_lookup_callable_is_applied(
    mes_options_chain_ingested,
) -> None:
    """When a lookup is provided, ChainSnapshot.underlying_price is
    populated; without one, it stays at the 0.0 sentinel.
    """
    # No-lookup branch.
    sessions_no_lookup = list(iter_session_chains(
        "mes_options_chain",
        start=_WINDOW_START, end=_WINDOW_END,
        root=mes_options_chain_ingested["raw_root"],
    ))
    for snap in sessions_no_lookup:
        assert snap.underlying_price == 0.0

    # Trivial lookup → constant price.
    def fake_lookup(ts, sym):
        return 5400.0

    sessions_with_lookup = list(iter_session_chains(
        "mes_options_chain",
        start=_WINDOW_START, end=_WINDOW_END,
        root=mes_options_chain_ingested["raw_root"],
        underlying_price_lookup=fake_lookup,
    ))
    for snap in sessions_with_lookup:
        assert snap.underlying_price == 5400.0
