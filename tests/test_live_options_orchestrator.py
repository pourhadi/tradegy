"""Tests for the daily paper-trade orchestrator.

Real-data tests using the ingested SPY chain (no synthetic chain
data per the no-synthetic-data memory). The orchestrator's
behavior over real chain data is what matters; mocking the chain
would defeat the validation.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from tradegy import config
from tradegy.live.options_orchestrator import (
    LiveDecision,
    build_validated_portfolio,
    generate_live_decision,
    write_decision,
)
from tradegy.options.strategies import (
    IronCondor45dteD16,
    IvGatedStrategy,
    JadeLizard45dte,
    PutCreditSpread45dteD30,
)


# ── build_validated_portfolio: pure composition ────────────────────


def test_build_no_gate_returns_bases_unchanged():
    bases = [PutCreditSpread45dteD30(), IronCondor45dteD16()]
    out = build_validated_portfolio(
        base_strategies=bases,
        iv_gate_max=None, iv_gate_min=None,
    )
    assert len(out) == 2
    assert out[0] is bases[0]
    assert out[1] is bases[1]


def test_build_iv_gate_max_wraps_each_base():
    bases = [PutCreditSpread45dteD30(), JadeLizard45dte()]
    out = build_validated_portfolio(
        base_strategies=bases,
        iv_gate_max=0.25, iv_gate_min=None,
        iv_gate_window_days=252,
    )
    assert len(out) == 2
    for s in out:
        assert isinstance(s, IvGatedStrategy)
        assert s.max_iv_rank == 0.25
        assert s.window_days == 252


def test_build_iv_gate_window_propagated():
    out = build_validated_portfolio(
        base_strategies=[PutCreditSpread45dteD30()],
        iv_gate_max=0.30, iv_gate_min=None,
        iv_gate_window_days=63,
    )
    assert out[0].window_days == 63


# ── generate_live_decision: end-to-end against real SPY chain ──────


@pytest.fixture
def spy_chain_present():
    """Skip the integration test when SPY chain isn't ingested.

    Per the no-synthetic-data rule, we don't fabricate snapshots —
    real data or skip the test.
    """
    raw_root = config.repo_root() / "data" / "raw"
    if not (raw_root / "source=spy_options_chain").exists():
        pytest.skip(
            "SPY chain not ingested; pull and ingest first:\n"
            "  python /Users/dan/code/data/download_spx_options_orats.py "
            "--ticker SPY --start 2025-12-15 --end 2025-12-31 --confirm\n"
            "  uv run tradegy ingest "
            "/Users/dan/code/data/spy_options_orats/spy_options_orats.csv "
            "--source-id spy_options_chain"
        )


def test_generate_live_decision_runs_on_real_spy(
    spy_chain_present,
):
    """End-to-end: replay every ingested SPY snapshot, generate
    today's entry candidates with the validated config.

    Asserts the decision is well-formed and matches the orchestrator
    contract — does NOT assert specific entries (data-dependent).
    """
    bases = [
        PutCreditSpread45dteD30(),
        IronCondor45dteD16(),
        JadeLizard45dte(),
    ]
    decision = generate_live_decision(
        base_strategies=bases,
        source_id="spy_options_chain",
        ticker="SPY",
        declared_capital=25_000.0,
        iv_gate_max=0.25,
    )
    assert isinstance(decision, LiveDecision)
    assert decision.underlying == "SPY"
    assert decision.source_id == "spy_options_chain"
    assert decision.declared_capital == 25_000.0
    assert decision.iv_gate_max == 0.25
    assert decision.n_replayed_snapshots > 0
    assert decision.snapshot_underlying_price > 0
    assert len(decision.strategy_ids) == 3
    # Each strategy_id should be the WRAPPED form.
    for sid in decision.strategy_ids:
        assert sid.startswith("iv_gated_max")
    # Entries may be empty (gate may block today) — both are valid.
    assert isinstance(decision.entries, list)


def test_generate_live_decision_entries_have_well_formed_legs(
    spy_chain_present,
):
    """When entries ARE produced, each entry's legs round-trip as
    a valid serialized order: strike float, expiry ISO date, side
    in {call, put}, quantity ±1 typically.
    """
    decision = generate_live_decision(
        base_strategies=[PutCreditSpread45dteD30(), IronCondor45dteD16()],
        source_id="spy_options_chain",
        ticker="SPY",
        declared_capital=25_000.0,
        iv_gate_max=0.30,  # looser gate → more likely to fire
    )
    if not decision.entries:
        pytest.skip("no entries produced today; can't validate leg shape")
    for entry in decision.entries:
        assert "strategy_id" in entry
        assert entry["contracts"] >= 1
        assert len(entry["legs"]) >= 2  # multi-leg by definition
        for leg in entry["legs"]:
            assert isinstance(leg["strike"], float)
            assert leg["side"] in {"call", "put"}
            assert leg["quantity"] in {-2, -1, 1, 2}
            # Expiry should parse as an ISO date.
            date.fromisoformat(leg["expiry"])


# ── write_decision: persistence ────────────────────────────────────


def test_compute_position_statuses_empty_registry(tmp_path, spy_chain_present):
    """Empty registry → empty statuses list. No error."""
    from tradegy.live.options_orchestrator import compute_position_statuses
    from tradegy.options.chain_io import iter_chain_snapshots
    from tradegy.options.strategy import ManagementRules

    raw_root = config.repo_root() / "data" / "raw"
    snap = list(iter_chain_snapshots(
        "spy_options_chain", ticker="SPY", root=raw_root,
    ))[-1]
    rules = ManagementRules()
    out = compute_position_statuses(
        snapshot=snap, rules=rules, registry_root=tmp_path,
    )
    assert out == []


def test_compute_position_statuses_dte_trigger(tmp_path, spy_chain_present):
    """A backfilled position with a long-past expiry triggers DTE
    close → PositionStatus.triggered_close_reason mentions dte_close.
    """
    import json
    from tradegy.live.options_backfill import backfill_from_file
    from tradegy.live.options_orchestrator import compute_position_statuses
    from tradegy.options.chain_io import iter_chain_snapshots
    from tradegy.options.strategy import ManagementRules

    raw_root = config.repo_root() / "data" / "raw"
    snap = list(iter_chain_snapshots(
        "spy_options_chain", ticker="SPY", root=raw_root,
    ))[-1]

    # Backfill a position with expiry well in the past — DTE < 0.
    backfill_data = [{
        "position_id": "status_dte_test",
        "underlying": "SPY",
        "strategy_class": "x",
        "contracts": 1,
        "entry_ts": "2020-01-15T20:46:00+00:00",
        "broker_order_id": "x",
        "legs": [
            {"expiry": "2020-04-17", "strike": 300.0, "side": "put",
             "quantity": -1, "entry_price": 2.50, "multiplier": 100},
            {"expiry": "2020-04-17", "strike": 295.0, "side": "put",
             "quantity": +1, "entry_price": 1.20, "multiplier": 100},
        ],
    }]
    json_path = tmp_path / "bf.json"
    json_path.write_text(json.dumps(backfill_data))
    backfill_from_file(json_path=json_path, registry_root=tmp_path)

    rules = ManagementRules(profit_take_pct=0.50, loss_stop_pct=2.0, dte_close=21)
    out = compute_position_statuses(
        snapshot=snap, rules=rules, registry_root=tmp_path,
    )
    assert len(out) == 1
    st = out[0]
    assert st.position_id == "status_dte_test"
    assert st.days_to_expiry < 0  # well in the past
    assert st.triggered_close_reason is not None
    assert "dte_close" in st.triggered_close_reason
    # Leg summary renders both legs.
    assert "P-1@K300" in st.leg_summary
    assert "P+1@K295" in st.leg_summary


def test_write_decision_round_trips_json(tmp_path, spy_chain_present):
    """Decision is written under <root>/<snap_date>_<wallclock>.json
    and the JSON parses back to dict-shaped data.
    """
    decision = generate_live_decision(
        base_strategies=[PutCreditSpread45dteD30()],
        source_id="spy_options_chain",
        ticker="SPY",
        declared_capital=25_000.0,
        iv_gate_max=0.25,
    )
    out_path = write_decision(decision, root=tmp_path)
    assert out_path.exists()
    assert out_path.suffix == ".json"
    payload = json.loads(out_path.read_text())
    # Top-level fields land in the JSON.
    assert payload["underlying"] == "SPY"
    assert payload["source_id"] == "spy_options_chain"
    assert payload["iv_gate_max"] == 0.25
    assert isinstance(payload["entries"], list)
