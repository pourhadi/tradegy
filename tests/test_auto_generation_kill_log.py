"""Kill-log injector tests.

Covers the three "killed" classifications, derived-reason synthesis,
and prompt rendering. Uses tmp_path-rooted fixtures so the tests are
independent of the live `hypotheses/` and `data/auto_generation/`
directories.
"""
from __future__ import annotations

from pathlib import Path

from tradegy.auto_generation.kill_log import (
    KilledHypothesisSummary,
    _is_killed,
    _summarize_variants,
    format_kill_summaries,
    load_kill_summaries,
)
from tradegy.auto_generation.records import (
    GateOutcome,
    GateResults,
    VariantOutcome,
    VariantRecord,
    VariantStats,
    append_record,
    now_utc_iso,
)


def _hyp_yaml(
    *,
    hyp_id: str = "hyp_test",
    status: str = "proposed",
    title: str = "Test hypothesis",
    mechanism: str = "",
    kill_reason: str = "",
) -> str:
    body = f"""
id: {hyp_id}
title: {title}
source: human
created_date: 2026-05-01
last_modified_date: 2026-05-01
author: t
status: {status}
mechanism: {mechanism!r}
"""
    if kill_reason:
        body += f"kill_reason: {kill_reason!r}\n"
    return body


def _record(
    *,
    hyp_id: str,
    variant_id: str,
    outcome: VariantOutcome,
    fail_reason: str = "",
) -> VariantRecord:
    return VariantRecord(
        variant_id=variant_id,
        hypothesis_id=hyp_id,
        generated_at=now_utc_iso(),
        generator_id="test",
        generator_metadata={},
        spec_id=variant_id,
        spec_hash="x",
        spec_version="0.1.0",
        budget_used=1,
        budget_cap=4,
        gate_results=GateResults(sanity=GateOutcome.FAILED),
        stats=VariantStats(),
        outcome=outcome,
        fail_reason=fail_reason,
    )


# ── _is_killed classification ───────────────────────────────────


def test_is_killed_explicit_status_killed():
    from tradegy.auto_generation.hypothesis import Hypothesis
    h = Hypothesis(
        id="x", title="t", source="human",
        created_date="2026-05-01", last_modified_date="2026-05-01",
        author="a", status="killed",
    )
    assert _is_killed(h, []) is True


def test_is_killed_status_retired():
    from tradegy.auto_generation.hypothesis import Hypothesis
    h = Hypothesis(
        id="x", title="t", source="human",
        created_date="2026-05-01", last_modified_date="2026-05-01",
        author="a", status="retired",
    )
    assert _is_killed(h, []) is True


def test_is_killed_all_variants_discarded():
    from tradegy.auto_generation.hypothesis import Hypothesis
    h = Hypothesis(
        id="x", title="t", source="human",
        created_date="2026-05-01", last_modified_date="2026-05-01",
        author="a", status="promoted",
    )
    records = [
        _record(
            hyp_id="x", variant_id=f"x__v{i}",
            outcome=VariantOutcome.DISCARDED_AT_SANITY,
        )
        for i in range(3)
    ]
    assert _is_killed(h, records) is True


def test_not_killed_when_some_variant_passed():
    from tradegy.auto_generation.hypothesis import Hypothesis
    h = Hypothesis(
        id="x", title="t", source="human",
        created_date="2026-05-01", last_modified_date="2026-05-01",
        author="a", status="promoted",
    )
    records = [
        _record(
            hyp_id="x", variant_id="x__v1",
            outcome=VariantOutcome.DISCARDED_AT_SANITY,
        ),
        _record(
            hyp_id="x", variant_id="x__v2",
            outcome=VariantOutcome.PASSED,
        ),
    ]
    assert _is_killed(h, records) is False


def test_not_killed_with_no_records_and_proposed_status():
    from tradegy.auto_generation.hypothesis import Hypothesis
    h = Hypothesis(
        id="x", title="t", source="human",
        created_date="2026-05-01", last_modified_date="2026-05-01",
        author="a", status="proposed",
    )
    assert _is_killed(h, []) is False


# ── _summarize_variants ─────────────────────────────────────────


def test_summarize_picks_dominant_outcome():
    records = [
        _record(
            hyp_id="x", variant_id=f"x__v{i}",
            outcome=VariantOutcome.DISCARDED_AT_SANITY,
            fail_reason="sanity: trades=0",
        )
        for i in range(5)
    ] + [
        _record(
            hyp_id="x", variant_id="x__v5",
            outcome=VariantOutcome.DISCARDED_AT_WALK_FORWARD,
        ),
    ]
    s = _summarize_variants(records)
    assert "5/6 variants discarded_at_sanity" in s
    assert "trades=0" in s


def test_summarize_empty_records():
    assert _summarize_variants([]) == "no variant records on disk"


# ── load_kill_summaries integration ─────────────────────────────


def test_load_kill_summaries_combines_yaml_and_records(tmp_path: Path):
    hyp_root = tmp_path / "hypotheses"
    var_root = tmp_path / "auto_generation"
    hyp_root.mkdir()

    # one hypothesis with status: killed
    (hyp_root / "hyp_a.yaml").write_text(_hyp_yaml(
        hyp_id="hyp_a", status="killed",
        title="A killed hypothesis",
        mechanism="alpha mechanism",
        kill_reason="sanity gate; 0 trades across 4 variants",
    ))

    # one promoted hypothesis with all variants discarded
    (hyp_root / "hyp_b.yaml").write_text(_hyp_yaml(
        hyp_id="hyp_b", status="promoted",
        title="B all-discarded hypothesis",
        mechanism="beta mechanism",
    ))
    for i in range(2):
        append_record(_record(
            hyp_id="hyp_b", variant_id=f"hyp_b__v{i}",
            outcome=VariantOutcome.DISCARDED_AT_SANITY,
            fail_reason="sanity: trades=0",
        ), root=var_root)

    # one promoted hypothesis with a passed variant — NOT killed
    (hyp_root / "hyp_c.yaml").write_text(_hyp_yaml(
        hyp_id="hyp_c", status="promoted",
        title="C survives",
    ))
    append_record(_record(
        hyp_id="hyp_c", variant_id="hyp_c__v1",
        outcome=VariantOutcome.PASSED,
    ), root=var_root)

    out = load_kill_summaries(
        hypotheses_root=hyp_root, variants_root=var_root,
    )
    ids = [s.hypothesis_id for s in out]
    assert "hyp_a" in ids
    assert "hyp_b" in ids
    assert "hyp_c" not in ids
    a = next(s for s in out if s.hypothesis_id == "hyp_a")
    assert "sanity gate" in a.derived_kill_reason  # uses YAML kill_reason verbatim
    b = next(s for s in out if s.hypothesis_id == "hyp_b")
    assert "discarded_at_sanity" in b.derived_kill_reason  # synthesized


def test_load_kill_summaries_empty_root(tmp_path: Path):
    out = load_kill_summaries(
        hypotheses_root=tmp_path / "missing",
        variants_root=tmp_path / "missing",
    )
    assert out == []


# ── Rendering ───────────────────────────────────────────────────


def test_format_empty_returns_empty_string():
    assert format_kill_summaries([]) == ""


def test_format_renders_title_and_mechanism():
    s = KilledHypothesisSummary(
        hypothesis_id="hyp_demo",
        title="Demo killed hypothesis",
        mechanism="some mechanism",
        derived_kill_reason="all variants discarded at sanity",
        n_variants_recorded=3,
        status="killed",
    )
    out = format_kill_summaries([s])
    assert "Demo killed hypothesis" in out
    assert "do not propose mechanistic near-duplicates" in out
    assert "some mechanism" in out
    assert "all variants discarded at sanity" in out


def test_format_caps_at_max_entries():
    summaries = [
        KilledHypothesisSummary(
            hypothesis_id=f"hyp_{i}", title=f"T{i}",
            mechanism="m", derived_kill_reason="r",
            n_variants_recorded=1, status="killed",
        )
        for i in range(50)
    ]
    out = format_kill_summaries(summaries, max_entries=5)
    titles = [line for line in out.splitlines() if line.startswith("- **T")]
    assert len(titles) == 5
