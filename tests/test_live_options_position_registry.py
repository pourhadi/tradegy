"""Tests for the local options-position registry.

Pure-data tests — append/load round-trip, close detection, file
format. No broker, no chain data needed.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from tradegy.live.options_position_registry import (
    PersistedLeg,
    append_close,
    append_open,
    load_open_positions,
    registry_path,
)
from tradegy.options.chain import OptionSide
from tradegy.options.positions import MultiLegPosition


def _put_credit_spread_legs() -> list[PersistedLeg]:
    """Realistic 2-leg PCS structure: short K480 put + long K475 put,
    same expiry. Pure dataclass shape — no chain data fabricated.
    """
    return [
        PersistedLeg(
            expiry="2026-04-17", strike=480.0, side="put",
            quantity=-1, entry_price=2.50, multiplier=100,
        ),
        PersistedLeg(
            expiry="2026-04-17", strike=475.0, side="put",
            quantity=+1, entry_price=1.20, multiplier=100,
        ),
    ]


def test_registry_path_creates_parent_dir(tmp_path):
    """Default path creation is idempotent and produces a file
    under data/live_options/.
    """
    p = registry_path(root=tmp_path)
    assert p.parent.exists()
    assert p.name == "positions.jsonl"


def test_append_open_round_trips(tmp_path):
    """Append an open row → load_open_positions returns a
    MultiLegPosition with matching shape.
    """
    append_open(
        position_id="pcs_test_1",
        underlying="SPY",
        strategy_class="iv_gated_max0.25_put_credit_spread_45dte_d30",
        contracts=1,
        legs=_put_credit_spread_legs(),
        entry_credit_per_share=1.30,
        max_loss_per_contract=370.0,
        entry_ts=datetime(2026, 3, 1, 21, 0, tzinfo=timezone.utc),
        broker_order_id="ibkr_42",
        root=tmp_path,
    )
    out = load_open_positions(root=tmp_path)
    assert len(out) == 1
    pos = out[0]
    assert isinstance(pos, MultiLegPosition)
    assert pos.position_id == "pcs_test_1"
    assert pos.contracts == 1
    assert len(pos.legs) == 2
    assert pos.entry_credit_per_share == 1.30
    assert pos.max_loss_per_contract == 370.0
    # Legs preserved with quantity signs.
    short_put = next(l for l in pos.legs if l.quantity == -1)
    long_put = next(l for l in pos.legs if l.quantity == +1)
    assert short_put.strike == 480.0
    assert long_put.strike == 475.0
    assert short_put.side == OptionSide.PUT


def test_append_close_filters_position_from_open_list(tmp_path):
    """Open-then-close → position no longer in load_open_positions."""
    append_open(
        position_id="pcs_test_2",
        underlying="SPY", strategy_class="x",
        contracts=1, legs=_put_credit_spread_legs(),
        entry_credit_per_share=1.30, max_loss_per_contract=370.0,
        entry_ts=datetime(2026, 3, 1, 21, 0, tzinfo=timezone.utc),
        broker_order_id="ibkr_43", root=tmp_path,
    )
    assert len(load_open_positions(root=tmp_path)) == 1
    append_close(
        position_id="pcs_test_2",
        closed_ts=datetime(2026, 4, 1, 21, 0, tzinfo=timezone.utc),
        closed_reason="dte_close: nearest leg at 16 DTE",
        closed_pnl_per_share=0.65,
        broker_close_order_id="ibkr_44",
        root=tmp_path,
    )
    assert load_open_positions(root=tmp_path) == []


def test_multiple_opens_and_closes_independent(tmp_path):
    """Two positions opened; close one → other stays open."""
    for pid in ("pcs_a", "pcs_b"):
        append_open(
            position_id=pid,
            underlying="SPY", strategy_class="x",
            contracts=1, legs=_put_credit_spread_legs(),
            entry_credit_per_share=1.30, max_loss_per_contract=370.0,
            entry_ts=datetime(2026, 3, 1, 21, 0, tzinfo=timezone.utc),
            broker_order_id=f"ibkr_{pid}", root=tmp_path,
        )
    assert len(load_open_positions(root=tmp_path)) == 2
    append_close(
        position_id="pcs_a",
        closed_ts=datetime(2026, 4, 1, 21, 0, tzinfo=timezone.utc),
        closed_reason="profit_take",
        root=tmp_path,
    )
    remaining = load_open_positions(root=tmp_path)
    assert len(remaining) == 1
    assert remaining[0].position_id == "pcs_b"


def test_jsonl_format_is_one_line_per_event(tmp_path):
    """Two opens + one close → 3 lines."""
    for pid in ("p1", "p2"):
        append_open(
            position_id=pid, underlying="SPY", strategy_class="x",
            contracts=1, legs=_put_credit_spread_legs(),
            entry_credit_per_share=1.30, max_loss_per_contract=370.0,
            entry_ts=datetime(2026, 3, 1, tzinfo=timezone.utc),
            broker_order_id="x", root=tmp_path,
        )
    append_close(
        position_id="p1",
        closed_ts=datetime(2026, 4, 1, tzinfo=timezone.utc),
        closed_reason="x", root=tmp_path,
    )
    path = registry_path(root=tmp_path)
    lines = path.read_text().strip().split("\n")
    assert len(lines) == 3
    # Each line is valid JSON with a "type" discriminator.
    for line in lines:
        row = json.loads(line)
        assert row["type"] in {"open", "close"}


def test_load_with_no_registry_returns_empty(tmp_path):
    """No file → empty list, no error."""
    assert load_open_positions(root=tmp_path) == []


def test_close_without_matching_open_is_silently_dropped(tmp_path):
    """Stray close row (no matching open) doesn't raise on load —
    it's just ignored. The open position list reflects truth as
    of the events seen.
    """
    append_close(
        position_id="never_opened",
        closed_ts=datetime.now(tz=timezone.utc),
        closed_reason="x", root=tmp_path,
    )
    # No exception, no spurious entries.
    assert load_open_positions(root=tmp_path) == []
