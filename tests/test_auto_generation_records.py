"""VariantRecord JSONL persistence tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from tradegy.auto_generation.records import (
    GateOutcome,
    GateResults,
    VariantOutcome,
    VariantRecord,
    VariantStats,
    append_record,
    read_records,
    variant_log_path,
)


def _record(
    *,
    variant_id: str = "v1",
    hypothesis_id: str = "hyp1",
    outcome: VariantOutcome = VariantOutcome.PASSED,
) -> VariantRecord:
    return VariantRecord(
        variant_id=variant_id,
        hypothesis_id=hypothesis_id,
        generated_at="2026-05-01T12:00:00+00:00",
        generator_id="stub",
        generator_metadata={"version": "1"},
        spec_id=variant_id,
        spec_hash="deadbeef",
        spec_version="0.1.0",
        budget_used=3,
        budget_cap=5,
        gate_results=GateResults(
            sanity=GateOutcome.PASSED, walk_forward=GateOutcome.PASSED,
        ),
        stats=VariantStats(raw_sharpe=0.5, total_trades=100),
        outcome=outcome,
        fail_reason="",
        sibling_variant_ids=("v1", "v2"),
    )


def test_append_and_read_round_trip(tmp_path: Path):
    rec = _record()
    append_record(rec, root=tmp_path)
    out = read_records("hyp1", root=tmp_path)
    assert len(out) == 1
    assert out[0].variant_id == "v1"
    assert out[0].gate_results.sanity == GateOutcome.PASSED
    assert out[0].stats.raw_sharpe == 0.5


def test_append_creates_parent_dirs(tmp_path: Path):
    p = tmp_path / "a" / "b"
    rec = _record()
    append_record(rec, root=p)
    assert (p / "hyp1" / "variants.jsonl").exists()


def test_read_empty_when_no_log(tmp_path: Path):
    out = read_records("never_logged", root=tmp_path)
    assert out == []


def test_multiple_records_preserve_order(tmp_path: Path):
    for i in range(3):
        append_record(
            _record(variant_id=f"v{i}", outcome=VariantOutcome.PASSED),
            root=tmp_path,
        )
    out = read_records("hyp1", root=tmp_path)
    assert [r.variant_id for r in out] == ["v0", "v1", "v2"]


def test_outcome_round_trips_each_value(tmp_path: Path):
    for o in VariantOutcome:
        append_record(
            _record(variant_id=f"v_{o.value}", outcome=o), root=tmp_path,
        )
    out = read_records("hyp1", root=tmp_path)
    assert {r.outcome for r in out} == set(VariantOutcome)


def test_variant_log_path_is_per_hypothesis(tmp_path: Path):
    a = variant_log_path("hyp_a", root=tmp_path)
    b = variant_log_path("hyp_b", root=tmp_path)
    assert a != b
    assert a.parent != b.parent  # per-hypothesis dir
