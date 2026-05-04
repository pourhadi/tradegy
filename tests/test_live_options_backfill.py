"""Tests for the registry backfill tool.

Pure-data tests — JSON input → registry side effect. No broker,
no chain data.
"""
from __future__ import annotations

import json

import pytest

from tradegy.live.options_backfill import backfill_from_file
from tradegy.live.options_position_registry import load_open_positions


def _valid_pcs_position(position_id: str = "backfill_pcs_001") -> dict:
    """A realistic 1-lot SPY put credit spread descriptor.

    Short K480 put @ $2.50, long K475 put @ $1.20 → net credit
    $1.30 per share. Wing width $5 × 100 mult = $500; max loss
    = ($5 - $1.30) × 100 = $370 per contract.
    """
    return {
        "position_id": position_id,
        "underlying": "SPY",
        "strategy_class": "iv_gated_max0.25_put_credit_spread_45dte_d30",
        "contracts": 1,
        "entry_ts": "2026-04-15T20:46:00+00:00",
        "broker_order_id": "ibkr_98765",
        "legs": [
            {"expiry": "2026-05-15", "strike": 480.0, "side": "put",
             "quantity": -1, "entry_price": 2.50, "multiplier": 100},
            {"expiry": "2026-05-15", "strike": 475.0, "side": "put",
             "quantity": +1, "entry_price": 1.20, "multiplier": 100},
        ],
    }


def test_backfill_appends_open_row(tmp_path):
    """Round-trip: JSON in → registry has the position visible
    via load_open_positions().
    """
    json_path = tmp_path / "positions.json"
    json_path.write_text(json.dumps([_valid_pcs_position()]))
    results = backfill_from_file(
        json_path=json_path, registry_root=tmp_path,
    )
    assert len(results) == 1
    assert results[0].appended is True
    assert results[0].computed_entry_credit_per_share == pytest.approx(1.30)
    # max_loss = (5 - 1.30) * 100 = 370.0
    assert results[0].computed_max_loss_per_contract == pytest.approx(370.0)
    # Registry has the position now.
    open_positions = load_open_positions(root=tmp_path)
    assert len(open_positions) == 1
    assert open_positions[0].position_id == "backfill_pcs_001"
    assert open_positions[0].entry_credit_per_share == pytest.approx(1.30)


def test_backfill_skips_already_registered(tmp_path):
    """Re-running the same JSON is idempotent — second invocation
    reports already_in_registry, doesn't double-append.
    """
    json_path = tmp_path / "positions.json"
    json_path.write_text(json.dumps([_valid_pcs_position()]))
    backfill_from_file(json_path=json_path, registry_root=tmp_path)
    results = backfill_from_file(json_path=json_path, registry_root=tmp_path)
    assert results[0].appended is False
    assert results[0].skipped_reason == "already_in_registry"
    # Still only one registered position.
    assert len(load_open_positions(root=tmp_path)) == 1


def test_backfill_rejects_undefined_risk_shape(tmp_path):
    """If the leg structure produces non-positive max_loss (e.g.
    naked short with no protective long), backfill refuses.
    """
    bad = _valid_pcs_position("naked_short")
    bad["legs"] = [bad["legs"][0]]  # remove the long protective leg
    json_path = tmp_path / "positions.json"
    json_path.write_text(json.dumps([bad]))
    results = backfill_from_file(
        json_path=json_path, registry_root=tmp_path,
    )
    # `legs must be ≥2` validation hits first.
    assert results[0].appended is False
    assert "legs" in (results[0].skipped_reason or "")


def test_backfill_rejects_missing_required_fields(tmp_path):
    """Missing fields surface as a per-position failure, not an
    abort — other positions in the same file still process.
    """
    good = _valid_pcs_position("good_one")
    bad = {"position_id": "bad_one"}  # missing everything
    json_path = tmp_path / "positions.json"
    json_path.write_text(json.dumps([good, bad]))
    results = backfill_from_file(
        json_path=json_path, registry_root=tmp_path,
    )
    assert len(results) == 2
    # First one (good) goes through.
    assert results[0].appended is True
    # Second one (bad) fails with a clear missing-fields error.
    assert results[1].appended is False
    assert "missing required fields" in (results[1].skipped_reason or "")


def test_backfill_rejects_zero_quantity_leg(tmp_path):
    bad = _valid_pcs_position()
    bad["legs"][0]["quantity"] = 0
    json_path = tmp_path / "positions.json"
    json_path.write_text(json.dumps([bad]))
    results = backfill_from_file(
        json_path=json_path, registry_root=tmp_path,
    )
    assert results[0].appended is False
    assert "quantity=0" in (results[0].skipped_reason or "")


def test_backfill_rejects_non_list_top_level(tmp_path):
    """JSON file must be a list of positions, not a dict."""
    json_path = tmp_path / "positions.json"
    json_path.write_text(json.dumps({"not": "a list"}))
    with pytest.raises(ValueError, match="must be a top-level list"):
        backfill_from_file(json_path=json_path, registry_root=tmp_path)


def test_backfill_iron_condor_4_legs(tmp_path):
    """Realistic 4-leg iron condor backfill: short put + long put
    + short call + long call. All in same expiry. Cleanly registers
    with positive max_loss.
    """
    ic = {
        "position_id": "backfill_ic_001",
        "underlying": "SPY",
        "strategy_class": "iv_gated_max0.25_iron_condor_45dte_d16",
        "contracts": 1,
        "entry_ts": "2026-04-15T20:46:00+00:00",
        "broker_order_id": None,
        "legs": [
            # Put side: short K460 / long K455 (5-wide put wing)
            {"expiry": "2026-05-15", "strike": 460.0, "side": "put",
             "quantity": -1, "entry_price": 1.40, "multiplier": 100},
            {"expiry": "2026-05-15", "strike": 455.0, "side": "put",
             "quantity": +1, "entry_price": 0.80, "multiplier": 100},
            # Call side: short K500 / long K505 (5-wide call wing)
            {"expiry": "2026-05-15", "strike": 500.0, "side": "call",
             "quantity": -1, "entry_price": 1.20, "multiplier": 100},
            {"expiry": "2026-05-15", "strike": 505.0, "side": "call",
             "quantity": +1, "entry_price": 0.60, "multiplier": 100},
        ],
    }
    json_path = tmp_path / "ic.json"
    json_path.write_text(json.dumps([ic]))
    results = backfill_from_file(
        json_path=json_path, registry_root=tmp_path,
    )
    assert results[0].appended is True
    # Net credit per share = -((-1×1.40) + (+1×0.80) + (-1×1.20) + (+1×0.60))
    #                      = -(-1.40 + 0.80 - 1.20 + 0.60)
    #                      = -(-1.20) = +1.20
    assert results[0].computed_entry_credit_per_share == pytest.approx(1.20)
    # Worst loss is one wing fully ITM. Wing width $5; credit covers
    # both wings (since condor risk is one-sided). max_loss/contract
    # = ($5 - $1.20) × 100 = $380.
    assert results[0].computed_max_loss_per_contract == pytest.approx(380.0)
