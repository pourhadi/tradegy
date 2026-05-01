"""Risk-cap pre-flight tests.

Covers each rejection path in doc 11 §263-273 plus the live-state
counters update_on_fill keeps consistent.
"""
from __future__ import annotations

import pytest

from tradegy.execution.risk_caps import (
    CheckResult,
    RejectReason,
    RiskCaps,
    RiskState,
    pre_flight_check,
    reset_daily_counters,
    reset_weekly_counters,
    update_on_fill,
)
from tradegy.strategies.types import Order, OrderType, Side


def _order(qty: int = 1) -> Order:
    return Order(side=Side.LONG, type=OrderType.MARKET, quantity=qty, tag="test")


def _baseline_state(**overrides) -> RiskState:
    base = dict(
        broker_connected=True,
        broker_last_heartbeat_age_s=0.5,
        within_trading_hours=True,
        realized_pnl_today=0.0,
        open_pnl=0.0,
        realized_pnl_this_week=0.0,
        open_position_count=0,
        open_instances_per_strategy={},
        available_margin=100_000.0,
        estimated_margin_per_contract=0.0,
        auto_disabled_strategies=frozenset(),
    )
    base.update(overrides)
    return RiskState(**base)


def _baseline_caps(**overrides) -> RiskCaps:
    base = dict(
        daily_loss_cap_dollars=500.0,
        weekly_loss_cap_dollars=2000.0,
        max_concurrent_positions_total=5,
        max_concurrent_instances_per_strategy=1,
        heartbeat_max_age_s=2.0,
        kill_switch_active=False,
        enforce_proposal_only_tier_block=True,
    )
    base.update(overrides)
    return RiskCaps(**base)


def _check(state: RiskState, caps: RiskCaps, **kwargs):
    defaults = dict(
        order=_order(),
        strategy_id="demo",
        strategy_tier="auto_execute",
        state=state,
        caps=caps,
    )
    defaults.update(kwargs)
    return pre_flight_check(**defaults)


def test_baseline_passes():
    r = _check(_baseline_state(), _baseline_caps())
    assert r.passed
    assert r.reason is None


def test_kill_switch_short_circuits_first():
    # Even with everything else broken, kill-switch reason wins.
    state = _baseline_state(broker_connected=False)
    caps = _baseline_caps(kill_switch_active=True)
    r = _check(state, caps)
    assert not r.passed
    assert r.reason == RejectReason.KILL_SWITCH_ACTIVE


def test_broker_disconnected_rejects():
    r = _check(_baseline_state(broker_connected=False), _baseline_caps())
    assert r.reason == RejectReason.BROKER_DISCONNECTED


def test_stale_heartbeat_rejects():
    r = _check(
        _baseline_state(broker_last_heartbeat_age_s=5.0), _baseline_caps()
    )
    assert r.reason == RejectReason.BROKER_DISCONNECTED


def test_outside_trading_hours_rejects():
    r = _check(_baseline_state(within_trading_hours=False), _baseline_caps())
    assert r.reason == RejectReason.OUTSIDE_TRADING_HOURS


def test_daily_loss_cap_breach_rejects():
    state = _baseline_state(realized_pnl_today=-500.0, open_pnl=-1.0)
    r = _check(state, _baseline_caps())
    assert r.reason == RejectReason.DAILY_LOSS_CAP_BREACH


def test_daily_loss_cap_at_floor_passes():
    # Exactly at the floor (-500.00 + 0.0 == -500.0) is still ≤ floor → fail.
    # Just above (-499.99) passes.
    state = _baseline_state(realized_pnl_today=-499.99)
    r = _check(state, _baseline_caps())
    assert r.passed


def test_weekly_loss_cap_breach_rejects():
    state = _baseline_state(realized_pnl_this_week=-2001.0)
    r = _check(state, _baseline_caps())
    assert r.reason == RejectReason.WEEKLY_LOSS_CAP_BREACH


def test_max_concurrent_positions_total_rejects():
    state = _baseline_state(open_position_count=5)
    r = _check(state, _baseline_caps(max_concurrent_positions_total=5))
    assert r.reason == RejectReason.MAX_CONCURRENT_POSITIONS


def test_max_concurrent_instances_per_strategy_rejects():
    state = _baseline_state(open_instances_per_strategy={"demo": 1})
    r = _check(state, _baseline_caps(max_concurrent_instances_per_strategy=1))
    assert r.reason == RejectReason.MAX_CONCURRENT_INSTANCES


def test_max_concurrent_instances_other_strategy_passes():
    state = _baseline_state(open_instances_per_strategy={"other": 1})
    r = _check(state, _baseline_caps(max_concurrent_instances_per_strategy=1))
    assert r.passed


def test_insufficient_margin_rejects():
    state = _baseline_state(
        available_margin=100.0, estimated_margin_per_contract=200.0,
    )
    # Order qty 1 needs 200 margin; only 100 available.
    r = _check(state, _baseline_caps())
    assert r.reason == RejectReason.INSUFFICIENT_MARGIN


def test_margin_check_skipped_when_estimate_unset():
    # estimated_margin_per_contract == 0 means broker hasn't returned a
    # value yet (or feature disabled). Skip the check, don't fail.
    state = _baseline_state(
        available_margin=0.0, estimated_margin_per_contract=0.0,
    )
    r = _check(state, _baseline_caps())
    assert r.passed


def test_auto_disabled_strategy_rejects():
    state = _baseline_state(auto_disabled_strategies=frozenset({"demo"}))
    r = _check(state, _baseline_caps())
    assert r.reason == RejectReason.STRATEGY_DISABLED


def test_proposal_only_tier_rejects():
    r = _check(_baseline_state(), _baseline_caps(), strategy_tier="proposal_only")
    assert r.reason == RejectReason.PROPOSAL_ONLY_TIER


def test_proposal_only_tier_can_be_disabled():
    caps = _baseline_caps(enforce_proposal_only_tier_block=False)
    r = _check(_baseline_state(), caps, strategy_tier="proposal_only")
    assert r.passed


def test_optional_caps_disable_their_check():
    caps = _baseline_caps(
        daily_loss_cap_dollars=None,
        weekly_loss_cap_dollars=None,
        max_concurrent_positions_total=None,
        max_concurrent_instances_per_strategy=None,
    )
    state = _baseline_state(
        realized_pnl_today=-10_000_000.0,
        realized_pnl_this_week=-10_000_000.0,
        open_position_count=999,
        open_instances_per_strategy={"demo": 999},
    )
    r = _check(state, caps)
    assert r.passed


# ─── update_on_fill ───────────────────────────────────────────────


def test_update_on_fill_open_increments_count():
    s = _baseline_state()
    s2 = update_on_fill(s, strategy_id="demo", position_delta=+1)
    assert s2.open_position_count == 1
    assert s2.open_instances_per_strategy == {"demo": 1}


def test_update_on_fill_close_decrements_count_and_records_pnl():
    s = _baseline_state(
        open_position_count=1, open_instances_per_strategy={"demo": 1},
    )
    s2 = update_on_fill(
        s, strategy_id="demo", position_delta=-1,
        pnl_realized_delta=+125.0,
    )
    assert s2.open_position_count == 0
    assert s2.open_instances_per_strategy == {"demo": 0}
    assert s2.realized_pnl_today == 125.0
    assert s2.realized_pnl_this_week == 125.0


def test_update_on_fill_floor_at_zero():
    s = _baseline_state(open_position_count=0)
    s2 = update_on_fill(s, strategy_id="demo", position_delta=-1)
    assert s2.open_position_count == 0


def test_reset_daily_keeps_weekly():
    s = _baseline_state(
        realized_pnl_today=-50.0,
        open_pnl=-12.0,
        realized_pnl_this_week=-200.0,
    )
    s2 = reset_daily_counters(s)
    assert s2.realized_pnl_today == 0.0
    assert s2.open_pnl == 0.0
    assert s2.realized_pnl_this_week == -200.0


def test_reset_weekly_clears_only_weekly():
    s = _baseline_state(
        realized_pnl_today=-50.0, realized_pnl_this_week=-200.0,
    )
    s2 = reset_weekly_counters(s)
    assert s2.realized_pnl_this_week == 0.0
    assert s2.realized_pnl_today == -50.0
