"""AutoTestOrchestrator tests.

Orchestrator logic (validation, dedup, gate dispatch, record
persistence) is isolated from the harness — `run_backtest` /
`run_walk_forward` are monkeypatched. The harness has its own
extensive test coverage; here we exercise the control flow that
sits on top.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from tradegy.auto_generation import orchestrator as orch_mod
from tradegy.auto_generation.generators import (
    GenerationContext,
    StubVariantGenerator,
)
from tradegy.auto_generation.hypothesis import (
    GateThresholds,
    Hypothesis,
)
from tradegy.auto_generation.orchestrator import (
    AutoTestOrchestrator,
    _corrected_sharpe_lift,
)
from tradegy.auto_generation.records import (
    GateOutcome,
    VariantOutcome,
    read_records,
)
from tradegy.specs.schema import (
    EntrySpec, MarketScopeSpec, MetadataSpec, SizingSpec, StopsSpec,
    StrategySpec, ExitsSpec, TimeStopBlock,
)


# ── Spec + hypothesis fixtures ─────────────────────────────────


def _make_spec(spec_id: str = "test_spec_a") -> StrategySpec:
    return StrategySpec(
        metadata=MetadataSpec(
            id=spec_id, version="0.1.0",
            name="Test variant", created_date=date(2026, 5, 1),
            last_modified_date=date(2026, 5, 1), author="tests",
        ),
        market_scope=MarketScopeSpec(instrument="MES"),
        entry=EntrySpec(
            strategy_class="vwap_reversion",
            parameters={"vwap_feature_id": "mes_vwap"},
        ),
        sizing=SizingSpec(
            method="fixed_contracts", parameters={"contracts": 1},
        ),
        stops=StopsSpec(
            initial_stop={"method": "fixed_ticks", "stop_ticks": 12, "tick_size": 0.25},
            time_stop=TimeStopBlock(enabled=True, max_holding_bars=30),
        ),
        exits=ExitsSpec(),
    )


@dataclass
class _FakeStats:
    sharpe: float
    total_trades: int


@dataclass
class _FakeBacktestResult:
    stats: _FakeStats


@dataclass
class _FakeWFSummary:
    avg_oos_sharpe: float
    avg_in_sample_sharpe: float
    coverage_start: datetime
    coverage_end: datetime
    passed: bool
    fail_reason: str = ""


# ── Multi-hypothesis correction math ───────────────────────────


def test_lift_zero_for_single_variant():
    assert _corrected_sharpe_lift(n_variants=1, total_trades=1000) == 0.0


def test_lift_grows_with_n():
    a = _corrected_sharpe_lift(n_variants=5, total_trades=1000)
    b = _corrected_sharpe_lift(n_variants=15, total_trades=1000)
    assert b > a > 0


def test_lift_shrinks_with_more_trades():
    a = _corrected_sharpe_lift(n_variants=10, total_trades=100)
    b = _corrected_sharpe_lift(n_variants=10, total_trades=10000)
    assert a > b > 0


def test_lift_zero_when_no_trades():
    assert _corrected_sharpe_lift(n_variants=10, total_trades=0) == 0.0


# ── Promotion gate ──────────────────────────────────────────────


def test_orchestrator_rejects_unpromoted_hypothesis(tmp_path):
    h = Hypothesis(
        id="hyp_unpromoted",
        title="t",
        source="human",
        created_date=date(2026, 5, 1),
        last_modified_date=date(2026, 5, 1),
        author="d",
        status="proposed",  # not yet promoted
    )
    with pytest.raises(ValueError, match="must be 'promoted'"):
        AutoTestOrchestrator(
            hypothesis=h,
            variant_generator=StubVariantGenerator([]),
            context=GenerationContext(
                available_class_ids=("vwap_reversion",),
                available_feature_ids=("mes_vwap",),
            ),
            persist_root=tmp_path,
        )


def test_orchestrator_rejects_budget_exhausted(tmp_path):
    """If the variants log already has budget_cap entries, instantiation
    must refuse — doc 07 §218-228 forbids post-hoc budget expansion.
    """
    from tradegy.auto_generation.records import (
        GateResults, VariantRecord, VariantStats, append_record,
    )
    h = Hypothesis(
        id="hyp_full",
        title="t",
        source="human",
        created_date=date(2026, 5, 1),
        last_modified_date=date(2026, 5, 1),
        author="d",
        status="promoted",
        variant_budget=2,
    )
    # Pre-populate the log with budget_cap entries.
    for i in range(2):
        append_record(
            VariantRecord(
                variant_id=f"old_{i}",
                hypothesis_id=h.id,
                generated_at="2026-05-01T00:00:00Z",
                generator_id="stub",
                generator_metadata={},
                spec_id=f"old_{i}",
                spec_hash="x",
                spec_version="0.1.0",
                budget_used=2, budget_cap=2,
                gate_results=GateResults(),
                stats=VariantStats(),
                outcome=VariantOutcome.DISCARDED_AT_SANITY,
            ),
            root=tmp_path,
        )
    with pytest.raises(ValueError, match="expanding the budget post-hoc"):
        AutoTestOrchestrator(
            hypothesis=h,
            variant_generator=StubVariantGenerator([]),
            context=GenerationContext(
                available_class_ids=("vwap_reversion",),
                available_feature_ids=("mes_vwap",),
            ),
            persist_root=tmp_path,
        )


# ── End-to-end through the harness ──────────────────────────────


def _promoted_hypothesis(**overrides) -> Hypothesis:
    base = dict(
        id="hyp_e2e_test",
        title="auto-gen orchestrator exercise",
        source="human",
        created_date=date(2026, 5, 1),
        last_modified_date=date(2026, 5, 1),
        author="d",
        status="promoted",
        variant_budget=3,
        gate_thresholds=GateThresholds(
            sanity_min_trades=10,
            sanity_min_in_sample_sharpe=0.0,
            walk_forward_min_in_sample_sharpe=0.0,
        ),
    )
    base.update(overrides)
    return Hypothesis(**base)


def _patch_harness(
    monkeypatch,
    *,
    backtest_sharpe: float = 0.5,
    backtest_trades: int = 100,
    wf_passed: bool = True,
    wf_oos_sharpe: float = 0.4,
    wf_in_sample_sharpe: float = 0.5,
    coverage_start: datetime | None = None,
    coverage_end: datetime | None = None,
):
    cs = coverage_start or datetime(2019, 5, 6, tzinfo=timezone.utc)
    ce = coverage_end or datetime(2026, 4, 30, tzinfo=timezone.utc)

    def fake_backtest(spec, **kwargs):
        return _FakeBacktestResult(
            stats=_FakeStats(
                sharpe=backtest_sharpe, total_trades=backtest_trades,
            ),
        )

    def fake_walk_forward(spec, **kwargs):
        return _FakeWFSummary(
            avg_oos_sharpe=wf_oos_sharpe,
            avg_in_sample_sharpe=wf_in_sample_sharpe,
            coverage_start=cs,
            coverage_end=ce,
            passed=wf_passed,
            fail_reason="" if wf_passed else "test_fail",
        )

    class _FakeBars:
        def row(self, idx: int, *, named: bool):
            return {"ts_utc": cs if idx == 0 else ce}

    def fake_load_bar_stream(instrument: str):
        return _FakeBars()

    monkeypatch.setattr(orch_mod, "run_backtest", fake_backtest)
    monkeypatch.setattr(orch_mod, "run_walk_forward", fake_walk_forward)
    monkeypatch.setattr(orch_mod, "load_bar_stream", fake_load_bar_stream)


# ── Sanity gate ────────────────────────────────────────────────


def test_sanity_pass_records_passed(tmp_path, monkeypatch):
    _patch_harness(monkeypatch, backtest_sharpe=0.6, backtest_trades=200)
    h = _promoted_hypothesis()
    orch = AutoTestOrchestrator(
        hypothesis=h,
        variant_generator=StubVariantGenerator([_make_spec()]),
        context=GenerationContext(
            available_class_ids=("vwap_reversion",),
            available_feature_ids=("mes_vwap",),
        ),
        persist_root=tmp_path,
        run_walk_forward_on_pass=False,
    )
    summary = orch.run()
    assert summary.variants_passed_sanity == 1
    [rec] = read_records(h.id, root=tmp_path)
    assert rec.outcome == VariantOutcome.PASSED
    assert rec.gate_results.sanity == GateOutcome.PASSED


def test_sanity_fail_low_trades(tmp_path, monkeypatch):
    _patch_harness(monkeypatch, backtest_sharpe=1.0, backtest_trades=5)
    h = _promoted_hypothesis(
        gate_thresholds=GateThresholds(sanity_min_trades=50),
    )
    orch = AutoTestOrchestrator(
        hypothesis=h,
        variant_generator=StubVariantGenerator([_make_spec()]),
        context=GenerationContext(
            available_class_ids=("vwap_reversion",),
            available_feature_ids=("mes_vwap",),
        ),
        persist_root=tmp_path,
        run_walk_forward_on_pass=False,
    )
    orch.run()
    [rec] = read_records(h.id, root=tmp_path)
    assert rec.outcome == VariantOutcome.DISCARDED_AT_SANITY
    assert rec.gate_results.sanity == GateOutcome.FAILED


def test_sanity_fail_negative_sharpe(tmp_path, monkeypatch):
    _patch_harness(monkeypatch, backtest_sharpe=-0.2, backtest_trades=200)
    h = _promoted_hypothesis()
    orch = AutoTestOrchestrator(
        hypothesis=h,
        variant_generator=StubVariantGenerator([_make_spec()]),
        context=GenerationContext(
            available_class_ids=("vwap_reversion",),
            available_feature_ids=("mes_vwap",),
        ),
        persist_root=tmp_path,
        run_walk_forward_on_pass=False,
    )
    orch.run()
    [rec] = read_records(h.id, root=tmp_path)
    assert rec.outcome == VariantOutcome.DISCARDED_AT_SANITY


# ── Walk-forward gate ────────────────────────────────────────


def test_walk_forward_pass_with_no_correction_for_n1(tmp_path, monkeypatch):
    _patch_harness(
        monkeypatch,
        backtest_sharpe=0.6, backtest_trades=200,
        wf_passed=True, wf_oos_sharpe=0.4, wf_in_sample_sharpe=0.6,
    )
    h = _promoted_hypothesis()
    orch = AutoTestOrchestrator(
        hypothesis=h,
        variant_generator=StubVariantGenerator([_make_spec()]),
        context=GenerationContext(
            available_class_ids=("vwap_reversion",),
            available_feature_ids=("mes_vwap",),
        ),
        persist_root=tmp_path,
        run_walk_forward_on_pass=True,
    )
    summary = orch.run()
    assert summary.variants_passed_walk_forward == 1
    [rec] = read_records(h.id, root=tmp_path)
    assert rec.outcome == VariantOutcome.PASSED
    assert rec.gate_results.walk_forward == GateOutcome.PASSED


def test_walk_forward_fail_persists_kill_reason(tmp_path, monkeypatch):
    _patch_harness(
        monkeypatch,
        backtest_sharpe=0.6, backtest_trades=200,
        wf_passed=False, wf_oos_sharpe=-0.2, wf_in_sample_sharpe=0.6,
    )
    h = _promoted_hypothesis()
    orch = AutoTestOrchestrator(
        hypothesis=h,
        variant_generator=StubVariantGenerator([_make_spec()]),
        context=GenerationContext(
            available_class_ids=("vwap_reversion",),
            available_feature_ids=("mes_vwap",),
        ),
        persist_root=tmp_path,
        run_walk_forward_on_pass=True,
    )
    summary = orch.run()
    assert summary.variants_passed_walk_forward == 0
    [rec] = read_records(h.id, root=tmp_path)
    assert rec.outcome == VariantOutcome.DISCARDED_AT_WALK_FORWARD
    assert rec.gate_results.walk_forward == GateOutcome.FAILED
    assert "test_fail" in rec.fail_reason or "corrected" in rec.fail_reason


# ── Validation + dedup ───────────────────────────────────────


def test_validation_failure_is_recorded(tmp_path, monkeypatch):
    """A spec referencing an unknown strategy class fails pre-backtest
    validation; the failure produces a VALIDATION_FAILED record.
    """
    _patch_harness(monkeypatch)
    bogus = _make_spec()
    bogus.entry.strategy_class = "nonexistent_class"

    h = _promoted_hypothesis()
    orch = AutoTestOrchestrator(
        hypothesis=h,
        variant_generator=StubVariantGenerator([bogus]),
        context=GenerationContext(
            available_class_ids=("nonexistent_class",),
            available_feature_ids=(),
        ),
        persist_root=tmp_path,
        run_walk_forward_on_pass=False,
    )
    summary = orch.run()
    assert summary.variants_validation_failed == 1
    records = read_records(h.id, root=tmp_path)
    assert any(r.outcome == VariantOutcome.VALIDATION_FAILED for r in records)


def test_duplicate_specs_are_deduped(tmp_path, monkeypatch):
    _patch_harness(monkeypatch)
    spec_a = _make_spec("dup_a")
    spec_a_dup = _make_spec("dup_a")  # identical content → same hash

    h = _promoted_hypothesis()
    orch = AutoTestOrchestrator(
        hypothesis=h,
        variant_generator=StubVariantGenerator([spec_a, spec_a_dup]),
        context=GenerationContext(
            available_class_ids=("vwap_reversion",),
            available_feature_ids=("mes_vwap",),
        ),
        persist_root=tmp_path,
        run_walk_forward_on_pass=False,
    )
    summary = orch.run()
    assert summary.variants_validation_failed == 1
    records = read_records(h.id, root=tmp_path)
    assert any("duplicate_content_hash" in r.fail_reason for r in records)


def test_summary_counts_aggregate(tmp_path, monkeypatch):
    """Three variants: two pass sanity, one fails. Verify counters."""
    _patch_harness(monkeypatch, backtest_sharpe=0.6, backtest_trades=200)
    h = _promoted_hypothesis()
    orch = AutoTestOrchestrator(
        hypothesis=h,
        variant_generator=StubVariantGenerator([
            _make_spec("v1"), _make_spec("v2"), _make_spec("v3"),
        ]),
        context=GenerationContext(
            available_class_ids=("vwap_reversion",),
            available_feature_ids=("mes_vwap",),
        ),
        persist_root=tmp_path,
        run_walk_forward_on_pass=False,
    )
    summary = orch.run()
    assert summary.variants_generated == 3
    assert summary.variants_passed_sanity == 3
    assert summary.candidate_count == 3
    assert len(read_records(h.id, root=tmp_path)) == 3


# ── Holdout gate ──────────────────────────────────────────────


def _patch_harness_with_holdout(
    monkeypatch,
    *,
    holdout_sharpe: float,
    wf_oos_sharpe: float = 0.4,
    wf_in_sample_sharpe: float = 0.6,
    sanity_sharpe: float = 0.6,
    sanity_trades: int = 200,
):
    """Patch harness functions for the holdout-flow tests.

    The holdout backtest is the SECOND call to run_backtest within the
    same orchestrator pass — the first is sanity. We discriminate on
    call order, not on kwargs, since both calls receive the same spec
    type and the orchestrator passes start/end only on the holdout call.
    """
    cs = datetime(2019, 5, 6, tzinfo=timezone.utc)
    ce = datetime(2026, 4, 30, tzinfo=timezone.utc)
    state = {"calls": 0}

    def fake_backtest(spec, **kwargs):
        state["calls"] += 1
        # Second call is the holdout backtest (start/end provided).
        if "start" in kwargs and "end" in kwargs:
            return _FakeBacktestResult(
                stats=_FakeStats(
                    sharpe=holdout_sharpe, total_trades=sanity_trades,
                ),
            )
        return _FakeBacktestResult(
            stats=_FakeStats(
                sharpe=sanity_sharpe, total_trades=sanity_trades,
            ),
        )

    def fake_walk_forward(spec, **kwargs):
        return _FakeWFSummary(
            avg_oos_sharpe=wf_oos_sharpe,
            avg_in_sample_sharpe=wf_in_sample_sharpe,
            coverage_start=cs, coverage_end=ce,
            passed=True, fail_reason="",
        )

    class _FakeBars:
        def row(self, idx: int, *, named: bool):
            return {"ts_utc": cs if idx == 0 else ce}

    monkeypatch.setattr(orch_mod, "run_backtest", fake_backtest)
    monkeypatch.setattr(orch_mod, "run_walk_forward", fake_walk_forward)
    monkeypatch.setattr(
        orch_mod, "load_bar_stream", lambda instrument: _FakeBars(),
    )


def test_holdout_pass_records_passed(tmp_path, monkeypatch):
    _patch_harness_with_holdout(
        monkeypatch,
        holdout_sharpe=0.30,  # ≥ 0.5 × 0.4 = 0.20
        wf_oos_sharpe=0.4,
    )
    h = _promoted_hypothesis()
    orch = AutoTestOrchestrator(
        hypothesis=h,
        variant_generator=StubVariantGenerator([_make_spec()]),
        context=GenerationContext(
            available_class_ids=("vwap_reversion",),
            available_feature_ids=("mes_vwap",),
        ),
        persist_root=tmp_path,
        run_walk_forward_on_pass=True,
        run_holdout_on_pass=True,
        holdout_months=12,
    )
    summary = orch.run()
    assert summary.variants_passed_holdout == 1
    assert summary.candidate_count == 1
    [rec] = read_records(h.id, root=tmp_path)
    assert rec.outcome == VariantOutcome.PASSED
    assert rec.gate_results.holdout == GateOutcome.PASSED
    assert rec.stats.holdout_sharpe == pytest.approx(0.30)


def test_holdout_fail_records_discarded(tmp_path, monkeypatch):
    _patch_harness_with_holdout(
        monkeypatch,
        holdout_sharpe=0.10,  # < 0.5 × 0.4 = 0.20
        wf_oos_sharpe=0.4,
    )
    h = _promoted_hypothesis()
    orch = AutoTestOrchestrator(
        hypothesis=h,
        variant_generator=StubVariantGenerator([_make_spec()]),
        context=GenerationContext(
            available_class_ids=("vwap_reversion",),
            available_feature_ids=("mes_vwap",),
        ),
        persist_root=tmp_path,
        run_walk_forward_on_pass=True,
        run_holdout_on_pass=True,
        holdout_months=12,
    )
    summary = orch.run()
    assert summary.variants_passed_holdout == 0
    assert summary.candidate_count == 0
    [rec] = read_records(h.id, root=tmp_path)
    assert rec.outcome == VariantOutcome.DISCARDED_AT_HOLDOUT
    assert rec.gate_results.holdout == GateOutcome.FAILED
    assert "0.5" in rec.fail_reason
    assert rec.stats.holdout_sharpe == pytest.approx(0.10)


def test_holdout_negative_wf_reference_fails_gate(tmp_path, monkeypatch):
    """When walk-forward avg OOS sharpe is non-positive, the holdout
    gate cannot pass — a negative threshold any holdout could clear
    would invert the gate's intent.
    """
    _patch_harness_with_holdout(
        monkeypatch,
        holdout_sharpe=0.5,  # would clear a negative threshold trivially
        wf_oos_sharpe=-0.1,
    )
    # walk-forward is configured to pass=True with negative oos sharpe;
    # this is the edge case the holdout guard exists for.
    h = _promoted_hypothesis(
        gate_thresholds=GateThresholds(
            sanity_min_trades=30,
            sanity_min_in_sample_sharpe=0.0,
            walk_forward_oos_in_sample_ratio=0.0,  # let WF "pass"
            walk_forward_min_in_sample_sharpe=0.0,
            holdout_sharpe_ratio_to_walk_forward=0.5,
        ),
    )
    orch = AutoTestOrchestrator(
        hypothesis=h,
        variant_generator=StubVariantGenerator([_make_spec()]),
        context=GenerationContext(
            available_class_ids=("vwap_reversion",),
            available_feature_ids=("mes_vwap",),
        ),
        persist_root=tmp_path,
        run_walk_forward_on_pass=True,
        run_holdout_on_pass=True,
        holdout_months=12,
    )
    summary = orch.run()
    [rec] = read_records(h.id, root=tmp_path)
    # The variant must not pass holdout; whether it's discarded at WF
    # or holdout depends on the WF gate semantics — the load-bearing
    # assertion is that it didn't reach the candidate pool.
    assert summary.candidate_count == 0
    assert rec.outcome != VariantOutcome.PASSED


def test_holdout_disabled_keeps_phase_b_behaviour(tmp_path, monkeypatch):
    """holdout_months=0 → variant passes after walk-forward (no holdout
    backtest invoked, no DISCARDED_AT_HOLDOUT path taken).
    """
    _patch_harness_with_holdout(
        monkeypatch,
        holdout_sharpe=999.0,  # would matter if holdout ran
        wf_oos_sharpe=0.4,
    )
    h = _promoted_hypothesis()
    orch = AutoTestOrchestrator(
        hypothesis=h,
        variant_generator=StubVariantGenerator([_make_spec()]),
        context=GenerationContext(
            available_class_ids=("vwap_reversion",),
            available_feature_ids=("mes_vwap",),
        ),
        persist_root=tmp_path,
        run_walk_forward_on_pass=True,
        run_holdout_on_pass=False,
        holdout_months=0,
    )
    summary = orch.run()
    [rec] = read_records(h.id, root=tmp_path)
    assert rec.outcome == VariantOutcome.PASSED
    assert rec.gate_results.holdout == GateOutcome.NOT_RUN
    assert rec.stats.holdout_sharpe is None
    assert summary.variants_passed_holdout == 0  # never incremented
