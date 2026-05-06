"""Tests for the live MES 0DTE paper-trading daemon.

Covers the pure helpers that don't need a live IBKR connection:

  - build_close_order_from_record flips entry quantities correctly
  - build_close_coid is deterministic per (entry_coid, reason)
  - kill_switch_active reflects file presence
  - mark_entry_closed updates the record atomically and is idempotent
  - load/write entry record round-trips

The async IBKR-talking flows (run_entry, run_management) are
integration-level and excluded here — they're tested by the
operator's smoke runs (`--dry-run`) against a live IB Gateway.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest


# ── Module loader ─────────────────────────────────────────────────


@pytest.fixture(scope="module")
def daemon():
    """Import scripts/live_mes_0dte.py as a module so we can call
    its helpers directly.  scripts/ isn't a package; load via
    importlib."""
    daemon_path = Path(__file__).resolve().parent.parent / "scripts" / "live_mes_0dte.py"
    spec = importlib.util.spec_from_file_location("live_mes_0dte", daemon_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["live_mes_0dte"] = module
    spec.loader.exec_module(module)
    return module


# ── Test data ────────────────────────────────────────────────────


def _sample_entry_record() -> dict:
    """Real-looking entry record for a Jun-3-2025 0DTE IC."""
    return {
        "session_date": "2025-06-03",
        "client_order_id": "mes_0dte_2025-06-03_v1",
        "broker_order_id": "12345",
        "ts_utc": "2025-06-03T14:30:00+00:00",
        "underlying_at_entry": 5400.0,
        "vix_prior_close": 19.5,
        "legs": [
            {"expiry": "2025-06-03", "strike": 5350.0, "side": "put",  "quantity": +1,
             "entry_bid": 1.00, "entry_ask": 1.20, "entry_mid": 1.10},
            {"expiry": "2025-06-03", "strike": 5375.0, "side": "put",  "quantity": -1,
             "entry_bid": 2.40, "entry_ask": 2.60, "entry_mid": 2.50},
            {"expiry": "2025-06-03", "strike": 5425.0, "side": "call", "quantity": -1,
             "entry_bid": 2.30, "entry_ask": 2.50, "entry_mid": 2.40},
            {"expiry": "2025-06-03", "strike": 5450.0, "side": "call", "quantity": +1,
             "entry_bid": 0.95, "entry_ask": 1.15, "entry_mid": 1.05},
        ],
        "tag": "mes_0dte_ic_25x25",
        "contracts": 1,
    }


# ── build_close_order_from_record ────────────────────────────────


def test_close_order_flips_quantities(daemon) -> None:
    record = _sample_entry_record()
    close = daemon.build_close_order_from_record(record)
    # Same number of legs.
    assert len(close.legs) == 4
    # Quantities flipped 1:1.
    expected_q = [-1, +1, +1, -1]   # entry was [+1,-1,-1,+1]
    actual_q = [leg.quantity for leg in close.legs]
    assert actual_q == expected_q


def test_close_order_preserves_strike_side_expiry(daemon) -> None:
    record = _sample_entry_record()
    close = daemon.build_close_order_from_record(record)
    for entry_leg, close_leg in zip(record["legs"], close.legs):
        assert close_leg.strike == entry_leg["strike"]
        assert close_leg.side.value == entry_leg["side"]
        assert close_leg.expiry == date.fromisoformat(entry_leg["expiry"])


def test_close_order_tag_is_close_suffix(daemon) -> None:
    record = _sample_entry_record()
    close = daemon.build_close_order_from_record(record)
    assert close.tag == "mes_0dte_ic_25x25_close"


def test_close_order_preserves_contracts(daemon) -> None:
    record = _sample_entry_record()
    record["contracts"] = 5
    close = daemon.build_close_order_from_record(record)
    assert close.contracts == 5


# ── build_close_coid ─────────────────────────────────────────────


def test_close_coid_is_deterministic(daemon) -> None:
    coid = daemon.build_close_coid("entry_v1", "profit_take")
    assert coid == "entry_v1_close_profit_take"


def test_close_coid_distinguishes_reasons(daemon) -> None:
    """Different close reasons must produce different coids so the
    router's idempotency check doesn't collide on a re-attempt
    with a different reason.
    """
    pt = daemon.build_close_coid("entry_v1", "profit_take")
    fc = daemon.build_close_coid("entry_v1", "force_close_eod")
    ks = daemon.build_close_coid("entry_v1", "kill_switch")
    assert len({pt, fc, ks}) == 3


# ── Kill-switch file ─────────────────────────────────────────────


def test_kill_switch_inactive_when_file_missing(daemon, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(daemon, "KILL_SWITCH_FILE", tmp_path / "nope")
    assert daemon.kill_switch_active() is False


def test_kill_switch_active_when_file_exists(daemon, tmp_path, monkeypatch) -> None:
    f = tmp_path / "kill"
    f.write_text("")
    monkeypatch.setattr(daemon, "KILL_SWITCH_FILE", f)
    assert daemon.kill_switch_active() is True


# ── Entry-record persistence ─────────────────────────────────────


def test_entry_record_roundtrips(daemon, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(daemon, "ENTRY_RECORDS_DIR", tmp_path)
    record = _sample_entry_record()
    sd = date.fromisoformat(record["session_date"])
    daemon.write_entry_record(record, sd)
    loaded = daemon.load_entry_record(sd)
    assert loaded == record


def test_load_entry_record_missing_returns_none(
    daemon, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(daemon, "ENTRY_RECORDS_DIR", tmp_path)
    assert daemon.load_entry_record(date(2099, 1, 1)) is None


def test_mark_entry_closed_updates_fields(
    daemon, tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(daemon, "ENTRY_RECORDS_DIR", tmp_path)
    record = _sample_entry_record()
    sd = date.fromisoformat(record["session_date"])
    daemon.write_entry_record(record, sd)

    close_ts = datetime(2025, 6, 3, 16, 0, tzinfo=timezone.utc)
    daemon.mark_entry_closed(
        sd,
        close_reason="profit_take",
        close_ts=close_ts,
        close_coid="entry_close_pt",
        close_legs=[{"strike": 5350, "close_mid": 0.5}],
        close_credit_per_share=-0.95,
        pnl_per_share=0.85,
    )
    updated = daemon.load_entry_record(sd)
    assert updated["closed"] is True
    assert updated["close_reason"] == "profit_take"
    assert updated["close_client_order_id"] == "entry_close_pt"
    assert updated["pnl_per_share"] == 0.85
    # Original entry-side fields preserved.
    assert updated["client_order_id"] == record["client_order_id"]


def test_prior_session_vix_close_signature(daemon, tmp_path, monkeypatch) -> None:
    """The VIX-gate helper must return (close_price, trade_date)
    so the entry job can compute staleness.  Regression: an earlier
    version returned just the float and we couldn't enforce a
    freshness gate.
    """
    import polars as pl
    from datetime import date as _date, datetime as _dt, timezone as _tz

    # Build a synthetic vix_daily layout under tmp_path.
    vix_root = tmp_path / "data" / "raw" / "source=vix_daily"
    for d, c in [(_date(2026, 4, 30), 19.5), (_date(2026, 5, 1), 20.0)]:
        part = vix_root / f"date={d.isoformat()}"
        part.mkdir(parents=True)
        df = pl.DataFrame({
            "ts_utc": [_dt(d.year, d.month, d.day, 20, 0, tzinfo=_tz.utc)],
            "open": [c],
            "high": [c],
            "low": [c],
            "close": [c],
        })
        df.write_parquet(part / "data.parquet")

    monkeypatch.setattr(daemon, "REPO_ROOT", tmp_path)
    result = daemon.prior_session_vix_close(_date(2026, 5, 6))
    assert result is not None
    close, prior_date = result
    assert close == 20.0
    assert prior_date == _date(2026, 5, 1)


# ── compute_close_cost / intrinsic-fallback regression ──────────


class _FakeSide:
    """Mimics OptionSide enum's `.value` attribute for the helper."""

    def __init__(self, value: str) -> None:
        self.value = value


class _FakeLeg:
    """Quoted leg shape the helper consumes.  Mirrors the live
    OptionLeg's shape (`side`, `bid`, `ask`) without importing the
    project's options module — keeps this test file usable from
    helpers tests alone."""

    def __init__(self, side: str, bid: float, ask: float) -> None:
        self.side = _FakeSide(side)
        self.bid = bid
        self.ask = ask


def test_compute_leg_close_mid_uses_quoted_mid_when_quotes_present(daemon) -> None:
    mid, used_intrinsic = daemon.compute_leg_close_mid(
        leg_side="call", strike=5400.0, bid=2.0, ask=2.4, cur_underlying=5450.0,
    )
    assert mid == pytest.approx(2.2)
    assert used_intrinsic is False


def test_compute_leg_close_mid_falls_back_to_intrinsic_when_quotes_collapse(
    daemon,
) -> None:
    """Both bid and ask zero — the canonical post-expiry quote
    collapse.  Must mark to intrinsic vs current underlying, not
    silently average to zero.  This is the regression for the
    2026-05-06 bug where the daemon recorded +$3.25 profit_take on
    an expired position.
    """
    # Call ITM by 8 points → intrinsic = 8.0.
    mid, used_intrinsic = daemon.compute_leg_close_mid(
        leg_side="call", strike=5400.0, bid=0.0, ask=0.0, cur_underlying=5408.0,
    )
    assert mid == pytest.approx(8.0)
    assert used_intrinsic is True


def test_compute_leg_close_mid_call_otm_intrinsic_is_zero(daemon) -> None:
    mid, used_intrinsic = daemon.compute_leg_close_mid(
        leg_side="call", strike=5400.0, bid=0.0, ask=0.0, cur_underlying=5395.0,
    )
    assert mid == pytest.approx(0.0)
    assert used_intrinsic is True


def test_compute_leg_close_mid_put_itm_intrinsic(daemon) -> None:
    # Put ITM by 5 points (underlying below strike).
    mid, used_intrinsic = daemon.compute_leg_close_mid(
        leg_side="put", strike=5400.0, bid=0.0, ask=0.0, cur_underlying=5395.0,
    )
    assert mid == pytest.approx(5.0)
    assert used_intrinsic is True


def test_compute_close_cost_full_ic_max_profit_at_expiry(daemon) -> None:
    """IC keeps full credit at expiry: underlying lands inside the
    short strikes and all legs collapse to bid=0/ask=0.  Without
    the intrinsic fallback this would falsely report close_cost=0
    and PnL = full credit, hiding any actual settlement loss.
    Here the underlying really is between strikes, so intrinsic IS
    zero for all legs and total close cost IS zero — the fallback
    just ensures that conclusion comes from the math, not from a
    quote-collapse coincidence.
    """
    legs = [
        # Long  put  K=5350 q=+1
        {"strike": 5350.0, "side": "put",  "quantity": +1, "entry_mid": 1.10},
        # Short put  K=5375 q=-1
        {"strike": 5375.0, "side": "put",  "quantity": -1, "entry_mid": 2.50},
        # Short call K=5425 q=-1
        {"strike": 5425.0, "side": "call", "quantity": -1, "entry_mid": 2.40},
        # Long  call K=5450 q=+1
        {"strike": 5450.0, "side": "call", "quantity": +1, "entry_mid": 1.05},
    ]
    snap = [
        _FakeLeg("put",  0.0, 0.0),
        _FakeLeg("put",  0.0, 0.0),
        _FakeLeg("call", 0.0, 0.0),
        _FakeLeg("call", 0.0, 0.0),
    ]
    close_cost, used_intrinsic = daemon.compute_close_cost(legs, snap, 5400.0)
    assert close_cost == pytest.approx(0.0)
    assert used_intrinsic == [True, True, True, True]


def test_compute_close_cost_full_ic_max_loss_at_expiry(daemon) -> None:
    """IC takes max loss when underlying breaks the short strike on
    one side.  Without the intrinsic fallback, both call-side legs
    would falsely register $0 (bid=0/ask=0 collapse) and the daemon
    would record +full credit instead of recognising the spread
    settled at max width.
    """
    legs = [
        {"strike": 5350.0, "side": "put",  "quantity": +1, "entry_mid": 1.10},
        {"strike": 5375.0, "side": "put",  "quantity": -1, "entry_mid": 2.50},
        {"strike": 5425.0, "side": "call", "quantity": -1, "entry_mid": 2.40},
        {"strike": 5450.0, "side": "call", "quantity": +1, "entry_mid": 1.05},
    ]
    snap = [
        _FakeLeg("put",  0.0, 0.0),
        _FakeLeg("put",  0.0, 0.0),
        _FakeLeg("call", 0.0, 0.0),
        _FakeLeg("call", 0.0, 0.0),
    ]
    # Underlying expires far above the long call → short call ITM
    # by 75 (5500 - 5425), long call ITM by 50 (5500 - 5450).
    # close_cost = -(-1)*75 + -(+1)*50 = 75 - 50 = 25.
    # Plus the put side: both worthless → 0 contribution.
    close_cost, _ = daemon.compute_close_cost(legs, snap, 5500.0)
    assert close_cost == pytest.approx(25.0)


def test_compute_close_cost_mixed_quotes_and_collapsed_quotes(daemon) -> None:
    """Mid-day case: some legs still quote normally, others have
    crossed into wide-spread territory.  The intrinsic fallback
    fires only on full bid/ask collapse, not on wide-but-real
    spreads.
    """
    legs = [
        {"strike": 5350.0, "side": "put",  "quantity": +1, "entry_mid": 1.10},
        {"strike": 5375.0, "side": "put",  "quantity": -1, "entry_mid": 2.50},
    ]
    snap = [
        _FakeLeg("put", 0.4, 0.6),    # quoted, mid 0.5
        _FakeLeg("put", 0.0, 0.0),    # collapsed → intrinsic
    ]
    # Cur underlying 5360 → second-leg intrinsic = max(0, 5375 - 5360) = 15.
    close_cost, used_intrinsic = daemon.compute_close_cost(legs, snap, 5360.0)
    # close_cost = -(+1)*0.5 + -(-1)*15 = -0.5 + 15 = 14.5
    assert close_cost == pytest.approx(14.5)
    assert used_intrinsic == [False, True]


def test_compute_close_cost_does_not_fall_back_on_one_sided_quotes(daemon) -> None:
    """One-sided quote (bid=0 ask>0 or bid>0 ask=0) is a wide
    market, not a quote collapse.  The fallback must NOT trigger
    or we'd silently override real broker pricing with intrinsic.
    """
    legs = [
        {"strike": 5400.0, "side": "call", "quantity": -1, "entry_mid": 2.0},
    ]
    snap = [
        _FakeLeg("call", 0.0, 0.5),  # bid 0, ask 0.5 — wide but real
    ]
    close_cost, used_intrinsic = daemon.compute_close_cost(legs, snap, 5395.0)
    # mid = 0.25; close_cost = -(-1)*0.25 = 0.25.  NOT intrinsic.
    assert close_cost == pytest.approx(0.25)
    assert used_intrinsic == [False]


def test_mark_entry_closed_is_idempotent(
    daemon, tmp_path, monkeypatch,
) -> None:
    """Re-marking a closed record overwrites the close metadata
    cleanly — useful if operator re-submits and we want the record
    to reflect the latest close.
    """
    monkeypatch.setattr(daemon, "ENTRY_RECORDS_DIR", tmp_path)
    record = _sample_entry_record()
    sd = date.fromisoformat(record["session_date"])
    daemon.write_entry_record(record, sd)
    close_ts = datetime(2025, 6, 3, 16, 0, tzinfo=timezone.utc)
    daemon.mark_entry_closed(
        sd, close_reason="profit_take", close_ts=close_ts,
        close_coid="c1", close_legs=[], close_credit_per_share=-0.5,
        pnl_per_share=0.5,
    )
    daemon.mark_entry_closed(
        sd, close_reason="kill_switch", close_ts=close_ts,
        close_coid="c2", close_legs=[], close_credit_per_share=-1.0,
        pnl_per_share=0.0,
    )
    updated = daemon.load_entry_record(sd)
    assert updated["close_reason"] == "kill_switch"
    assert updated["close_client_order_id"] == "c2"
