"""Automated strategy-generation pipeline.

Per `06_hypothesis_system.md` + `07_auto_generation.md`. The pipeline
turns a hypothesis (or a stream of generated hypotheses) into a
candidate pool of validated strategy variants without bypassing any
of the existing validation gates.

Stages:

  1. Hypothesis generation (LLM, optional)
       Source: literature scanner / market-structure monitor / human
       submission / etc., per `06_hypothesis_system.md` §79-153.
  2. Hypothesis enrichment + triage
       LLM articulates mechanism, scores feasibility, checks data
       deps; humans (or the system, scored) promote.
  3. Variant generation (LLM)
       Per doc 07: N variants from a promoted hypothesis using only
       registered classes / features / parameter envelopes.
  4. Auto-test
       AutoTestOrchestrator runs sanity → walk-forward → holdout per
       variant; Deflated Sharpe correction across the variant pool;
       persists VariantRecords.
  5. Candidate pool
       Survivors live as a JSONL log; humans pick which advance to
       manual CPCV / paper trading.

This package ships the framework (schemas, ABCs, orchestrator,
storage) with stub LLM implementations for tests. The Anthropic SDK
integrations live in `tradegy.auto_generation.anthropic_*` modules.
"""
from tradegy.auto_generation.generators import (
    HypothesisGenerator,
    StubHypothesisGenerator,
    StubVariantGenerator,
    VariantGenerator,
)
from tradegy.auto_generation.hypothesis import (
    Hypothesis,
    HypothesisStatus,
    load_hypothesis,
)
from tradegy.auto_generation.orchestrator import (
    AutoTestOrchestrator,
    AutoTestSummary,
)
from tradegy.auto_generation.records import (
    GateOutcome,
    VariantOutcome,
    VariantRecord,
    append_record,
    read_records,
)

__all__ = [
    "AutoTestOrchestrator",
    "AutoTestSummary",
    "GateOutcome",
    "Hypothesis",
    "HypothesisGenerator",
    "HypothesisStatus",
    "StubHypothesisGenerator",
    "StubVariantGenerator",
    "VariantGenerator",
    "VariantOutcome",
    "VariantRecord",
    "append_record",
    "load_hypothesis",
    "read_records",
]
