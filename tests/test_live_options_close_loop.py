"""Tests for the V2 close-side automation: broker-position fetch,
reconciliation, close-trigger evaluation.

The broker-position fetcher is exercised against a minimal duck-
typed `_FakeIB` since IBKR isn't available in the test environment;
this is testing OUR OWN parsing of IBKR's contract/position shape,
not testing IBKR itself. (The shape we expect is documented in
ib_async; constructing a Position-like duck is testing parsing
not generating synthetic market data.)

The close-trigger evaluator is exercised against the real ingested
SPY chain — we register a position via the registry, mark-to-
market against today's snapshot, and verify the should_close
trigger semantics match the backtest runner.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import pytest

from tradegy import config
from tradegy.live.options_close_loop import (
    BrokerOptionLeg,
    CloseDecision,
    evaluate_closes,
    fetch_broker_option_legs,
    reconcile,
)
from tradegy.live.options_position_registry import (
    PersistedLeg,
    append_open,
    load_open_positions,
)
from tradegy.options.chain import OptionSide
from tradegy.options.strategy import ManagementRules


# ── Broker-position fetch (parsing semantics) ───────────────────────


@dataclass
class _FakeContract:
    """Duck-type for ib_async.Contract. Pure parsing harness — we're
    testing OUR field-extraction code, not constructing market data.
    """
    secType: str
    symbol: str
    lastTradeDateOrContractMonth: str
    right: str
    strike: float
    multiplier: str = "100"


@dataclass
class _FakePosition:
    contract: _FakeContract
    position: int
    avgCost: float


class _FakeIB:
    def __init__(self, positions: list[_FakePosition]) -> None:
        self._positions = positions

    def positions(self) -> list[_FakePosition]:
        return self._positions


def test_fetch_returns_only_options_on_target_underlying():
    """Filters out non-OPT secTypes, filters out other underlyings."""
    fake = _FakeIB([
        _FakePosition(
            contract=_FakeContract(
                secType="OPT", symbol="SPY",
                lastTradeDateOrContractMonth="20260417",
                right="P", strike=480.0,
            ),
            position=-1, avgCost=250.0,  # IBKR returns per-contract; we /100
        ),
        _FakePosition(
            contract=_FakeContract(
                secType="STK", symbol="SPY",
                lastTradeDateOrContractMonth="",
                right="", strike=0.0,
            ),
            position=100, avgCost=480.0,
        ),
        _FakePosition(
            contract=_FakeContract(
                secType="OPT", symbol="QQQ",
                lastTradeDateOrContractMonth="20260417",
                right="P", strike=400.0,
            ),
            position=-1, avgCost=200.0,
        ),
    ])
    legs = fetch_broker_option_legs(fake, underlying="SPY")
    assert len(legs) == 1
    assert legs[0].underlying == "SPY"
    assert legs[0].strike == 480.0
    assert legs[0].side == OptionSide.PUT
    assert legs[0].quantity == -1
    # avg_cost normalized to per-share (250 / 100 = 2.50).
    assert legs[0].avg_cost == pytest.approx(2.50)


def test_fetch_handles_call_side_and_long_qty():
    fake = _FakeIB([
        _FakePosition(
            contract=_FakeContract(
                secType="OPT", symbol="SPY",
                lastTradeDateOrContractMonth="20260620",
                right="C", strike=520.0,
            ),
            position=+2, avgCost=180.0,
        ),
    ])
    legs = fetch_broker_option_legs(fake, underlying="SPY")
    assert len(legs) == 1
    assert legs[0].side == OptionSide.CALL
    assert legs[0].quantity == 2
    assert legs[0].expiry == date(2026, 6, 20)


def test_fetch_skips_unparseable_dates_and_unknown_rights():
    """Garbage data from a half-initialized contract → skip not raise."""
    fake = _FakeIB([
        _FakePosition(
            contract=_FakeContract(
                secType="OPT", symbol="SPY",
                lastTradeDateOrContractMonth="bad",  # unparseable
                right="P", strike=480.0,
            ),
            position=-1, avgCost=250.0,
        ),
        _FakePosition(
            contract=_FakeContract(
                secType="OPT", symbol="SPY",
                lastTradeDateOrContractMonth="20260417",
                right="X",  # unknown right
                strike=480.0,
            ),
            position=-1, avgCost=250.0,
        ),
    ])
    legs = fetch_broker_option_legs(fake, underlying="SPY")
    assert legs == []


# ── Reconciliation ─────────────────────────────────────────────────


def _registered_pcs(tmp_path) -> None:
    """Register a 1-lot SPY PCS (short K480 / long K475 puts)."""
    append_open(
        position_id="pcs_recon_1",
        underlying="SPY", strategy_class="x",
        contracts=1,
        legs=[
            PersistedLeg(
                expiry="2026-04-17", strike=480.0, side="put",
                quantity=-1, entry_price=2.50, multiplier=100,
            ),
            PersistedLeg(
                expiry="2026-04-17", strike=475.0, side="put",
                quantity=+1, entry_price=1.20, multiplier=100,
            ),
        ],
        entry_credit_per_share=1.30, max_loss_per_contract=370.0,
        entry_ts=datetime(2026, 3, 1, 21, 0, tzinfo=timezone.utc),
        broker_order_id="ibkr_x", root=tmp_path,
    )


def test_reconcile_matched_when_broker_has_both_legs(tmp_path):
    _registered_pcs(tmp_path)
    local = load_open_positions(root=tmp_path)
    broker = [
        BrokerOptionLeg(
            underlying="SPY", expiry=date(2026, 4, 17),
            strike=480.0, side=OptionSide.PUT, quantity=-1,
            avg_cost=2.50,
        ),
        BrokerOptionLeg(
            underlying="SPY", expiry=date(2026, 4, 17),
            strike=475.0, side=OptionSide.PUT, quantity=+1,
            avg_cost=1.20,
        ),
    ]
    report = reconcile(local_positions=local, broker_legs=broker)
    assert report.matched == local
    assert report.local_only == []
    assert report.broker_only == []
    assert not report.has_divergence


def test_reconcile_local_only_when_broker_missing_a_leg(tmp_path):
    """Broker shows the short put but NOT the long protective put.
    The position can't be matched → flagged local-only, NOT closed
    automatically (could be assignment / partial close).
    """
    _registered_pcs(tmp_path)
    local = load_open_positions(root=tmp_path)
    broker = [
        BrokerOptionLeg(
            underlying="SPY", expiry=date(2026, 4, 17),
            strike=480.0, side=OptionSide.PUT, quantity=-1,
            avg_cost=2.50,
        ),
    ]
    report = reconcile(local_positions=local, broker_legs=broker)
    assert report.matched == []
    assert len(report.local_only) == 1
    # The lone broker leg becomes broker-only (registered position
    # didn't consume it because match failed first).
    assert len(report.broker_only) == 1
    assert report.broker_only[0].strike == 480.0
    assert report.has_divergence


def test_reconcile_broker_only_when_position_opened_outside_system(tmp_path):
    """Operator opened a position via TWS directly. Registry has
    nothing; broker has it → flagged broker-only.
    """
    broker = [
        BrokerOptionLeg(
            underlying="SPY", expiry=date(2026, 4, 17),
            strike=520.0, side=OptionSide.CALL, quantity=-1,
            avg_cost=1.50,
        ),
    ]
    report = reconcile(local_positions=[], broker_legs=broker)
    assert report.matched == []
    assert report.local_only == []
    assert len(report.broker_only) == 1
    assert report.has_divergence


# ── Close-trigger evaluation against real SPY chain ────────────────


@pytest.fixture
def spy_chain_present():
    raw_root = config.repo_root() / "data" / "raw"
    if not (raw_root / "source=spy_options_chain").exists():
        pytest.skip("SPY chain not ingested; run `tradegy ingest` first")


def test_evaluate_closes_dte_trigger_against_real_chain(
    spy_chain_present, tmp_path,
):
    """A position whose nearest-leg DTE is ≤ rules.dte_close should
    trigger. We construct a position with a long-past expiry → DTE
    is negative → trigger fires.
    """
    from tradegy.options.chain_io import iter_chain_snapshots

    raw_root = config.repo_root() / "data" / "raw"
    snaps = list(iter_chain_snapshots(
        "spy_options_chain", ticker="SPY", root=raw_root,
    ))
    today = snaps[-1]

    # Register a position with an expiry well in the past — guaranteed
    # to trigger DTE close. We use a fake strike since only the DTE
    # check matters here; mark-to-market would fall back to intrinsic
    # but the DTE rule fires FIRST.
    past_expiry = "2020-04-17"  # well before any real today
    append_open(
        position_id="dte_test", underlying="SPY",
        strategy_class="x", contracts=1,
        legs=[
            PersistedLeg(
                expiry=past_expiry, strike=300.0, side="put",
                quantity=-1, entry_price=2.50, multiplier=100,
            ),
            PersistedLeg(
                expiry=past_expiry, strike=295.0, side="put",
                quantity=+1, entry_price=1.20, multiplier=100,
            ),
        ],
        entry_credit_per_share=1.30, max_loss_per_contract=370.0,
        entry_ts=datetime(2020, 3, 1, 21, 0, tzinfo=timezone.utc),
        broker_order_id="x", root=tmp_path,
    )
    positions = load_open_positions(root=tmp_path)
    rules = ManagementRules(profit_take_pct=0.50, loss_stop_pct=2.0, dte_close=21)
    closes = evaluate_closes(
        positions=positions, snapshot=today, rules=rules,
    )
    assert len(closes) == 1
    assert "dte_close" in closes[0].close_reason
    # The close order inverts each leg's quantity.
    co = closes[0].to_close_order()
    short_close = next(l for l in co.legs if l.strike == 300.0)
    long_close = next(l for l in co.legs if l.strike == 295.0)
    assert short_close.quantity == +1   # buying back the short
    assert long_close.quantity == -1    # selling the long
