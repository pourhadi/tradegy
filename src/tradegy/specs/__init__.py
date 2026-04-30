"""Strategy spec schema, loader, and validator.

Per 04_strategy_spec_schema.md, a strategy spec is the canonical YAML
contract between human authors, the harness that validates and runs it,
and the selection LLM that picks it at runtime.

This module implements the human-authored sections (`metadata`,
`market_scope`, `entry`, `sizing`, `stops`, `exits`, `parameter_envelope`,
`retirement_criteria`, `operational`). Harness-authored sections
(`backtest_evidence`, `validation_record`, `live_performance`) are
appended after the harness runs (Phase 3B onward).
"""
from __future__ import annotations

from tradegy.specs.loader import load_spec, validate_spec  # noqa: F401
from tradegy.specs.schema import (  # noqa: F401
    EntrySpec,
    ExitsSpec,
    MarketScopeSpec,
    MetadataSpec,
    OperationalSpec,
    ParameterEnvelopeSpec,
    RetirementCriteriaSpec,
    SizingSpec,
    StopsSpec,
    StrategySpec,
)
