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
from tradegy.auto_generation.anthropic_generators import (
    AnthropicHypothesisGenerator,
    AnthropicVariantGenerator,
    HypothesisDraft,
    HypothesisDraftBatch,
    StrategySpecBatch,
)
from tradegy.auto_generation.cost import (
    CostEstimate,
    cost_for_usage,
    format_cost_line,
)
from tradegy.auto_generation.feature_stats import (
    FeatureStats,
    compute_all_feature_stats,
    compute_feature_stats,
    format_feature_stats,
    read_stats,
    write_stats,
)
from tradegy.auto_generation.kill_log import (
    KilledHypothesisSummary,
    format_kill_summaries,
    load_kill_summaries,
)
from tradegy.auto_generation.market_scan import (
    MarketScanReport,
    Observation,
    compute_market_scan,
    format_market_scan_report,
    read_latest_market_scan,
    write_market_scan,
)
from tradegy.auto_generation.generators import (
    HypothesisGenerator,
    StubHypothesisGenerator,
    StubVariantGenerator,
    VariantGenerator,
)
from tradegy.auto_generation.hypothesis import (
    Hypothesis,
    HypothesisStatus,
    list_hypotheses,
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
    "AnthropicHypothesisGenerator",
    "AnthropicVariantGenerator",
    "AutoTestOrchestrator",
    "AutoTestSummary",
    "CostEstimate",
    "FeatureStats",
    "GateOutcome",
    "Hypothesis",
    "HypothesisDraft",
    "HypothesisDraftBatch",
    "HypothesisGenerator",
    "HypothesisStatus",
    "KilledHypothesisSummary",
    "MarketScanReport",
    "Observation",
    "StrategySpecBatch",
    "StubHypothesisGenerator",
    "StubVariantGenerator",
    "VariantGenerator",
    "VariantOutcome",
    "VariantRecord",
    "append_record",
    "compute_all_feature_stats",
    "compute_feature_stats",
    "compute_market_scan",
    "cost_for_usage",
    "format_cost_line",
    "format_feature_stats",
    "format_kill_summaries",
    "format_market_scan_report",
    "list_hypotheses",
    "load_hypothesis",
    "load_kill_summaries",
    "read_latest_market_scan",
    "read_records",
    "read_stats",
    "write_market_scan",
    "write_stats",
]
