"""Hypothesis record schema + loader.

Per `06_hypothesis_system.md` § Hypothesis queue schema + § Five-test
scorer. A hypothesis is a structured record carrying the mechanism,
falsification criteria, parameter envelope, and pre-registered
variant + gate budgets that doc 07 § Post-hoc rules require to be
fixed BEFORE generation.

Stored as YAML at `hypotheses/<id>.yaml`. The schema is small; the
fields that matter most for the auto-generator are:

  * `mechanism` — the WHY behind the hypothesis (LLM consumes this
    when drafting variants).
  * `falsification` — what observed result would refute the
    hypothesis. Doc 06 §14: hypotheses that cannot state what would
    refute them do not proceed.
  * `parameter_envelope` — declared bounds for any tunable variant
    parameter. Doc 07 §11: variants cannot use parameters outside
    these bounds.
  * `variant_budget` — pre-registered N (3-15 per §82-90).
  * `gate_thresholds` — pre-registered sanity / walk-forward / CPCV /
    holdout numerics. Locked at hypothesis-promotion time per §218-228.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from tradegy import config


HypothesisStatus = Literal[
    "proposed",
    "enriched",
    "triaged",
    "promoted",
    "killed",
    "in_test",
    "candidate",
    "manual_cpcv",
    "retired",
]


HypothesisSource = Literal[
    "human",
    "literature",
    "market_structure",
    "behavioral",
    "anomaly",
    "post_session_mining",
    "llm_brainstorm",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GateThresholds(_Strict):
    """Numeric gates locked at promotion time.

    Defaults match the doc 07 §163-179 starting values. Any tightening
    would be a new hypothesis (post-hoc relaxation is forbidden).
    """

    sanity_min_trades: int = 30
    sanity_min_in_sample_sharpe: float = 0.0
    walk_forward_oos_in_sample_ratio: float = 0.5
    walk_forward_min_in_sample_sharpe: float = 0.0
    cpcv_median_sharpe_threshold: float = 0.8
    cpcv_max_pct_paths_negative: float = 0.20
    holdout_sharpe_ratio_to_walk_forward: float = 0.5


class ParameterRangeSpec(_Strict):
    """Bounds for one tunable parameter the variant generator may
    explore. Mirrors the strategy-spec parameter envelope; the
    auto-gen variant validator enforces variants stay within.
    """

    name: str
    min: float
    max: float
    step: float | None = None


class FiveTestScores(_Strict):
    """Per `06_hypothesis_system.md` § Five-test scorer (lines 248+).

    Each axis is 1-5; the LLM-enrichment step fills these in. The
    triage decision is a function of the aggregate but the scores
    are persisted for audit / process review.
    """

    mechanism: int = Field(0, ge=0, le=5)
    falsifiability: int = Field(0, ge=0, le=5)
    novelty: int = Field(0, ge=0, le=5)
    economic_plausibility: int = Field(0, ge=0, le=5)
    data_feasibility: int = Field(0, ge=0, le=5)


class Hypothesis(_Strict):
    id: str
    title: str
    status: HypothesisStatus = "proposed"
    source: HypothesisSource
    created_date: date
    last_modified_date: date
    author: str

    observation: str = ""
    mechanism: str = ""
    """The WHY: who trades, why, under what constraints, what pattern
    therefore exists. Per doc 06 design principle 2: a hypothesis
    without a mechanism is a pattern observation, not a hypothesis.
    """

    falsification: str = ""
    """What observed result would refute it. Per doc 06 design
    principle 3: hypotheses that cannot state what would refute them
    do not proceed.
    """

    counterparty: str = ""
    """Optional but encouraged: who is the counterparty whose flow
    creates the edge."""

    instrument_scope: list[str] = Field(default_factory=list)
    """Instruments the hypothesis applies to (e.g., ["MES", "ES"]).
    The variant generator constrains spec.market_scope.instrument
    to one of these."""

    feature_dependencies: list[str] = Field(default_factory=list)
    """Registry feature ids the mechanism keys on. Empty means the
    generator picks from all admitted features. Populating this
    constrains the variant generator and is filled in during
    enrichment."""

    parameter_envelope: list[ParameterRangeSpec] = Field(default_factory=list)
    """Declared bounds for tunable parameters. Locked at promotion
    time per doc 07 §218-228."""

    variant_budget: int = Field(5, ge=1, le=15)
    """Pre-registered N; doc 07 §82-90 hard cap 15."""

    gate_thresholds: GateThresholds = Field(default_factory=GateThresholds)

    five_test_scores: FiveTestScores = Field(default_factory=FiveTestScores)

    parent_hypothesis_id: str | None = None
    """If this hypothesis is a refinement of another, link back."""

    notes: str = ""

    kill_reason: str | None = None
    """Set when status transitions to `killed`."""


def hypotheses_dir() -> Path:
    """Where hypothesis YAMLs live. Mirrors `strategy_specs_dir()`."""
    return config.repo_root() / "hypotheses"


def load_hypothesis(hypothesis_id: str, *, root: Path | None = None) -> Hypothesis:
    """Read `<root>/<hypothesis_id>.yaml` and validate via the
    Hypothesis schema. Raises FileNotFoundError or pydantic
    ValidationError on failure.
    """
    base = root or hypotheses_dir()
    path = base / f"{hypothesis_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"hypothesis YAML not found: {path}")
    raw = yaml.safe_load(path.read_text())
    return Hypothesis.model_validate(raw)


def list_hypotheses(*, root: Path | None = None) -> list[Hypothesis]:
    """Return every hypothesis YAML in `root` (default: hypotheses/),
    sorted by id. Pydantic-validates each.
    """
    base = root or hypotheses_dir()
    if not base.exists():
        return []
    out: list[Hypothesis] = []
    for path in sorted(base.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text())
        out.append(Hypothesis.model_validate(raw))
    return out
