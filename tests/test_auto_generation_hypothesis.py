"""Hypothesis schema + loader tests."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tradegy.auto_generation.hypothesis import (
    FiveTestScores,
    GateThresholds,
    Hypothesis,
    ParameterRangeSpec,
    list_hypotheses,
    load_hypothesis,
)


_BASE_FIELDS = dict(
    id="hyp_demo",
    title="Demo hypothesis",
    source="human",
    created_date=date(2026, 5, 1),
    last_modified_date=date(2026, 5, 1),
    author="dan",
)


def test_minimal_hypothesis_validates():
    h = Hypothesis(**_BASE_FIELDS)
    assert h.id == "hyp_demo"
    assert h.status == "proposed"
    assert h.variant_budget == 5
    assert h.gate_thresholds.cpcv_median_sharpe_threshold == 0.8


def test_variant_budget_capped_at_15():
    with pytest.raises(Exception):
        Hypothesis(**_BASE_FIELDS, variant_budget=16)


def test_variant_budget_at_least_1():
    with pytest.raises(Exception):
        Hypothesis(**_BASE_FIELDS, variant_budget=0)


def test_parameter_envelope_round_trip():
    envs = [
        ParameterRangeSpec(name="threshold", min=0.0, max=0.05, step=0.005),
        ParameterRangeSpec(name="lookback", min=5, max=30),
    ]
    h = Hypothesis(**_BASE_FIELDS, parameter_envelope=envs)
    assert len(h.parameter_envelope) == 2


def test_five_test_scores_bounded():
    with pytest.raises(Exception):
        Hypothesis(**_BASE_FIELDS, five_test_scores=FiveTestScores(mechanism=6))


def test_load_hypothesis_round_trip(tmp_path: Path):
    yaml_text = """
id: hyp_x
title: Test hypothesis
source: human
created_date: 2026-05-01
last_modified_date: 2026-05-01
author: dan
status: promoted
variant_budget: 3
gate_thresholds:
  sanity_min_trades: 40
  sanity_min_in_sample_sharpe: 0.0
  walk_forward_oos_in_sample_ratio: 0.5
  walk_forward_min_in_sample_sharpe: 0.0
  cpcv_median_sharpe_threshold: 0.8
  cpcv_max_pct_paths_negative: 0.20
  holdout_sharpe_ratio_to_walk_forward: 0.5
parameter_envelope:
  - name: threshold
    min: 0.001
    max: 0.01
"""
    (tmp_path / "hyp_x.yaml").write_text(yaml_text)
    h = load_hypothesis("hyp_x", root=tmp_path)
    assert h.id == "hyp_x"
    assert h.status == "promoted"
    assert h.variant_budget == 3
    assert h.gate_thresholds.sanity_min_trades == 40


def test_load_hypothesis_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_hypothesis("nope", root=tmp_path)


def test_list_hypotheses_returns_sorted(tmp_path: Path):
    for hid in ["hyp_b", "hyp_a", "hyp_c"]:
        (tmp_path / f"{hid}.yaml").write_text(f"""
id: {hid}
title: t
source: human
created_date: 2026-05-01
last_modified_date: 2026-05-01
author: d
""")
    out = list_hypotheses(root=tmp_path)
    assert [h.id for h in out] == ["hyp_a", "hyp_b", "hyp_c"]


def test_list_hypotheses_empty_when_dir_missing(tmp_path: Path):
    out = list_hypotheses(root=tmp_path / "does_not_exist")
    assert out == []


def test_kill_reason_optional():
    h = Hypothesis(**_BASE_FIELDS, status="killed", kill_reason="failed walk-forward")
    assert h.kill_reason == "failed walk-forward"


def test_extra_fields_forbidden():
    with pytest.raises(Exception):
        Hypothesis(**_BASE_FIELDS, made_up_field=True)
