"""Tests for the IBKR status → tradegy OrderState mapping."""
from __future__ import annotations

from tradegy.execution.ibkr_status import map_ibkr_status
from tradegy.execution.lifecycle import OrderState


def test_pending_submit_maps_to_submitted():
    m = map_ibkr_status("PendingSubmit", filled=0, total=1)
    assert m.target_state == OrderState.SUBMITTED
    assert not m.is_terminal_hint


def test_pre_submitted_maps_to_submitted():
    m = map_ibkr_status("PreSubmitted", filled=0, total=1)
    assert m.target_state == OrderState.SUBMITTED


def test_submitted_maps_to_working():
    m = map_ibkr_status("Submitted", filled=0, total=1)
    assert m.target_state == OrderState.WORKING


def test_full_fill_maps_to_filled():
    m = map_ibkr_status("Filled", filled=2, total=2)
    assert m.target_state == OrderState.FILLED
    assert m.is_terminal_hint


def test_partial_fill_maps_to_partial():
    m = map_ibkr_status("Filled", filled=1, total=3)
    assert m.target_state == OrderState.PARTIAL
    assert not m.is_terminal_hint


def test_filled_with_zero_qty_is_anomalous():
    m = map_ibkr_status("Filled", filled=0, total=2)
    assert m.target_state == OrderState.UNKNOWN
    assert "anomalous" in m.note


def test_filled_with_zero_total_is_anomalous():
    m = map_ibkr_status("Filled", filled=0, total=0)
    assert m.target_state == OrderState.UNKNOWN


def test_cancelled_maps_to_cancelled():
    m = map_ibkr_status("Cancelled", filled=0, total=1)
    assert m.target_state == OrderState.CANCELLED
    assert m.is_terminal_hint


def test_api_cancelled_maps_to_cancelled():
    m = map_ibkr_status("ApiCancelled", filled=0, total=1)
    assert m.target_state == OrderState.CANCELLED


def test_inactive_maps_to_rejected():
    m = map_ibkr_status("Inactive", filled=0, total=1)
    assert m.target_state == OrderState.REJECTED
    assert m.is_terminal_hint


def test_pending_cancel_holds_state():
    m = map_ibkr_status("PendingCancel", filled=0, total=1)
    assert m.target_state is None
    assert m.note == "hold"


def test_unknown_status_routes_to_unknown():
    m = map_ibkr_status("FooBarBaz", filled=0, total=1)
    assert m.target_state == OrderState.UNKNOWN
    assert "unrecognized" in m.note
