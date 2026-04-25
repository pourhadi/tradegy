# Futures Trading Platform — Design Documents

A document set defining the architecture, components, and processes for an ES-only automated futures trading platform built around LLM-supervised strategy selection and a deliberate, human-gated strategy development pipeline.

## Reading order

1. [`00_master_architecture.md`](00_master_architecture.md) — start here. The holistic picture and how components relate.
2. [`01_strategy_definition.md`](01_strategy_definition.md) — what a strategy is, identity rules, five required components.
3. [`02_feature_pipeline.md`](02_feature_pipeline.md) — upstream data/feature/model pipeline with a single admission bar for every data source.
4. [`03_strategy_class_registry.md`](03_strategy_class_registry.md) — code-level building blocks that specs compose.
5. [`04_strategy_spec_schema.md`](04_strategy_spec_schema.md) — the library entry contract.
6. [`05_backtest_harness.md`](05_backtest_harness.md) — deterministic execution engine with signed outputs.
7. [`06_hypothesis_system.md`](06_hypothesis_system.md) — how hypotheses are generated, triaged, and promoted.
8. [`07_auto_generation.md`](07_auto_generation.md) — automated variant generation with statistical guardrails.
9. [`08_development_pipeline.md`](08_development_pipeline.md) — 10-stage process from hypothesis to retirement.
10. [`09_selection_layer.md`](09_selection_layer.md) — runtime LLM decision logic.
11. [`10_review_gap_analysis.md`](10_review_gap_analysis.md) — independent review of gaps, feasibility, and recommended approach improvements.

## Status

All documents are drafts. Each doc has an "Open design decisions" section at the bottom listing questions still needing resolution before v1.0 freeze.

## Not yet documented

- Execution layer spec (IB API integration)
- Live monitoring spec
- Governance process doc
- Infrastructure/ops spec
- Example library entries (4–6 fully filled-in starter strategies)
