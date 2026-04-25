# Documentation Review — Gaps, Feasibility, Strategy, and Better Approaches

**Date:** 2026-04-24  
**Reviewer:** Codex

## Executive assessment

The document set is unusually strong at architecture clarity and process discipline. The core decisions are coherent:

- deterministic execution and tactical layers,
- LLM used for supervisory selection (not order-path control),
- explicit human gates for capital-affecting decisions,
- strategy and feature pipelines separated from runtime concerns.

That said, the docs are still missing several v1-critical contracts that currently exist only as intent. The largest risk is not conceptual quality, but **integration ambiguity** between runtime components (selection/tactical/execution) and between governance intent and enforceable mechanisms.

## What is solid today

1. **Architecture decomposition is clear and defensible** (strategic vs tactical vs execution). This should materially reduce failure coupling.
2. **Validation-first strategy pipeline** is well structured (hypothesis → evidence → promotion).
3. **Backtest harness concepts** correctly emphasize reproducibility, leakage controls, and signed evidence.
4. **Open decisions are documented explicitly**, which is better than implied assumptions.

## Key gaps (priority-ranked)

### P0 — Must resolve before live capital

#### 1) Missing execution-layer specification

The architecture depends on an execution layer for order lifecycle, broker reconciliation, and hard risk enforcement, but this is listed as “not yet documented.” Without this spec, all upstream guarantees are non-binding in production.

**Why this blocks feasibility:**
- No canonical behavior for rejects/partials/timeouts/cancel-replace.
- No documented failure semantics for IB outages or stale account state.
- No deterministic replay contract from tactical intents to exchange outcomes.

**Recommended artifact (minimum):**
- State machine for order lifecycle + idempotency keys.
- Broker reconciliation loop and divergence policy.
- Global kill-switch semantics.
- Session boundary behavior.

#### 2) Missing live monitoring and alerting spec

Multiple docs assume auto-disable, drift detection, and operational intervention, but alert thresholds, ownership, and escalation paths are undefined.

**Why this blocks feasibility:**
- No objective SLO/SLA for “system healthy enough to trade.”
- No operational playbook for degraded modes.

**Recommended artifact (minimum):**
- Health checks (data freshness, feature lag, model freshness, position/account reconciliation).
- Alert severity matrix and escalation chain.
- Auto-halt triggers with explicit cooldown/restart conditions.

#### 3) Missing concrete governance process document

Governance is articulated in principles/tables, but not in procedure.

**Why this blocks feasibility:**
- Approval authority, evidence package standards, and audit trail requirements are not formally operationalized.

**Recommended artifact (minimum):**
- RACI matrix.
- Promotion/revision checklist templates.
- Exception handling (urgent risk reductions, rollback authority).

### P1 — High leverage for v1 reliability

#### 4) Cross-document schema contracts need hardening

Several fields/behaviors are implied but not uniformly specified across strategy schema, class registry, and harness.

Examples:
- feature dependencies (raised in open decisions),
- warm-up semantics (harness open decision),
- gap handling semantics,
- confirm-then-execute payload.

**Better approach:** publish a single **Interface Control Document (ICD)** that maps each contract field to producer, consumer, validation owner, and failure behavior.

#### 5) Multi-strategy portfolio simulation deferred too far

Single-strategy validation is acceptable for early development, but promotion to live library-level behavior without portfolio interaction modeling will produce fragile expectations.

**Better approach:** keep single-strategy for early gates, but add a **minimal portfolio simulation mode** before multi-strategy live deployment:
- incompatibility rules,
- shared risk-cap collisions,
- correlated drawdown stress.

#### 6) Selection layer evaluation needs calibration protocol

The selection doc defines desirable metrics (coverage, precision, stand-down discipline), but lacks explicit baseline and offline calibration methodology.

**Better approach:** introduce an offline “decision replay” benchmark:
- fixed historical snapshots,
- blinded candidate sets,
- compare LLM decisions to deterministic heuristics and human review.

### P2 — Efficiency and scale improvements

#### 7) LLM cost-control is acknowledged but fragmented

Cost appears in hypothesis/auto-generation/selection docs but lacks a unified budget policy.

**Better approach:** define one budget controller with per-subsystem quotas, escalation rules, and fallback models.

#### 8) Data architecture choices are open too late

Feature pipeline still leaves storage/query architecture unresolved.

**Better approach:** decide now on hot/cold split and retention strategy; otherwise downstream APIs and SLAs will churn.

## Feasibility analysis

### Near-term feasibility (v1 single-instrument, small capital): **Yes, conditionally**

Feasible if P0 artifacts are completed before live deployment and P1 contract hardening is done before scaling library breadth.

### Main delivery risk

The highest risk is **integration debt**:
- well-written component docs,
- but missing operational contracts where components meet.

This typically fails during paper→live transition, not in backtests.

## Suggested implementation strategy (re-ordered)

Current sequencing is good conceptually, but should explicitly add control-plane artifacts earlier.

### Recommended revised critical path

1. Feature pipeline MVP (current).
2. Strategy class registry + schema validator (current).
3. Backtest harness MVP (current).
4. **Execution layer spec + monitoring spec + governance process doc** (move earlier).
5. One strategy through full dev pipeline to paper.
6. Selection layer MVP with replay benchmark (before live).
7. Live pilot with one strategy at `confirm_then_execute`.
8. Expand strategy count only after portfolio interaction simulation and monitoring burn-in.

## Decision checklist for v1 freeze

Before v1 freeze, enforce these as “must answer” decisions:

- Feature dependency declaration: required field? (recommend yes)
- Warm-up and gap semantics: where declared and enforced? (recommend strategy schema + harness enforcement)
- Reject/partial-fill handling: execution default or strategy-specific? (recommend execution default + optional strategy override field)
- Human override payload and UX contract: exact schema and TTL.
- LLM model policy by workflow: pre-session, intra-session, post-session, hypothesis enrichment, auto-generation.

## Alternative/better approaches worth considering

1. **Deterministic first-pass ranker before LLM scoring**  
   Use a lightweight rule-based or learned ranker to trim candidate set and reduce token pressure. Preserve LLM for final judgment and rationale.

2. **Library “champion/challenger” framework**  
   For each active strategy family, maintain one champion and one challenger in shadow mode to improve replacement discipline.

3. **Formal degraded-mode matrix**  
   Define behavior under dependency loss (LLM unavailable, feature stale, broker disconnect): continue/stand-down/flatten.

4. **Evidence packet standardization**  
   Introduce machine-readable evidence bundles for promotion reviews to reduce subjective drift in human approvals.

## Practical next steps (2-week doc sprint)

1. Write `execution_layer_spec.md` with state machines and failure semantics.
2. Write `live_monitoring_spec.md` with health checks and auto-halt policies.
3. Write `governance_process.md` with RACI and review templates.
4. Add an ICD appendix mapping fields across docs `03/04/05/09`.
5. Add one fully worked example strategy spec and one full harness evidence artifact.

## Bottom line

The strategic design is strong and internally consistent. The fastest path to real feasibility is to convert currently implicit runtime and governance assumptions into explicit, testable contracts. Once those are in place, the architecture is suitable for a controlled single-instrument live pilot.
