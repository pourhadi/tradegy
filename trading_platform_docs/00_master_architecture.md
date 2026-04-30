# Master Architecture

**Status:** Draft for review
**Purpose:** The holistic picture. How components relate. What the system is and isn't. This is the map; other documents are the territory.

---

## The system in one paragraph

A single-instrument (ES) futures trading platform built around a three-layer architecture: a deterministic tactical/execution layer that runs mechanical strategies, a strategic LLM-supervised layer that decides which strategies to activate and how to manage open positions, and a deliberate, human-gated pipeline that produces, validates, and retires the strategies the system can run. Speed is not an advantage; depth of reasoning and quality of selection are. The system is designed to make a small number of well-chosen trades consistently, not to compete on latency or volume.

---

## Starting constraints

- **Capital:** $10k starting account. Dictates instrument selection (MES over ES in most cases) and position sizing.
- **Market:** ES only. Soybeans dropped from the original plan. Future expansion considered NQ/RTY before returning to agricultural products.
- **Data:** Several years of 1-second OHLCV ES bars (Sierra Chart export, 2015–2023) and 5-second OHLCV MES bars (2019–2025) on hand. Both ingested as parity-contract sources (`es_1s_ohlcv`, `mes_5s_ohlcv`) with paired live IBKR adapters. Additional data sources onboarded through the feature pipeline's admission process (each source must meet the universal bar: point-in-time queryable, deterministic, declared availability latency, explicit revision policy, sufficient coverage, paired live adapter producing the canonical schema).
- **Broker:** Interactive Brokers via their trading API.
- **Latency stance:** LLM-driven decision cycles are 2–5 seconds. The architecture assumes this is fine because strategic reasoning is not on the critical path of any trade execution. We do not compete on speed.

---

## Architectural shift from the original plan

The original plan (`futures_trading_platform_plan_v4.md`) placed the LLM inside the decision pipeline — triggered at regime transitions and entry confirmations, with its output gating execution. That coupling made the LLM a dependency of every trade.

The current architecture treats the LLM as a **supervisory/strategic layer above** a deterministic trading system. The LLM decides what game the system is playing (strategy selection, playbook selection, position management) and writes the configuration that the deterministic layer runs against. The deterministic layer executes trades on its own within those configured rails. The LLM is not in the critical path of any individual trade.

This shift:
- Removes latency concerns from trade execution
- Decouples LLM failures from trade failures
- Makes strategies independently backtestable and evaluable
- Enables clean P&L attribution across layers
- Plays to the LLM's strengths (context integration, judgment) and away from its weaknesses (speed, determinism)

Parts of the original plan remain valid: the HMM regime model (now a feature, not a branching decision), the sentiment pipeline (now a feature family, not a parallel signal), the phase gates (now distributed across pipeline-specific gates), the risk management principles, the broker integration approach, the CPCV validation methodology. Parts superseded: the regime-transition protocol (subsumed into regular selection cycles), the LLM-gated entry confirmations (removed), the single-document monolithic plan structure (replaced by the document set below).

---

## The three layers

### Strategic layer (LLM-supervised, 5–10 min cadence + event triggers)

**Purpose:** Decide what the system is doing right now. Pick which library strategies are active. Manage open positions at the thesis level. Produce pre-session briefs and post-session reviews.

**Inputs:** Current market context (features, regime state, event proximity, cross-asset state), live library (strategies and their declared context conditions), open position state, portfolio risk state, previous cycle's decisions.

**Outputs:** Active playbook (which strategies are armed), stand-down flag, open position actions (hold/tighten/partial/exit), next reeval trigger, rationale, confidence.

**Mechanics:** Hard filtering (deterministic) → LLM scoring (structured output) → guardrail enforcement (deterministic) → apply to tactical layer.

**Not in the critical path of trade execution.** Cadence-based with event triggers for material context shifts. Detailed in `09_selection_layer.md`.

### Tactical layer (deterministic, real-time)

**Purpose:** Execute mechanical strategies against live market data. No intelligence — just rule execution. Each armed strategy runs as an isolated state machine through the lifecycle DORMANT → ARMED → ENTERING → IN_POSITION → EXITING → DONE_FOR_SESSION.

**Inputs:** Configuration from strategic layer (which strategies armed, with which parameters, under what risk envelope), real-time feature stream from feature pipeline, fill confirmations from execution layer.

**Outputs:** Orders submitted to execution layer, state updates broadcast to strategic layer.

**Key property:** deterministic. Same inputs produce identical outputs. Fully replayable against historical data. Has no access to LLMs at runtime.

### Execution layer (deterministic, broker integration)

**Purpose:** Order routing, fill management, position tracking, account state. No intelligence. Pure plumbing between tactical layer and Interactive Brokers.

**Scope includes:** order submission, cancellation, modification; fill reporting; position reconciliation; account balance tracking; margin monitoring; portfolio-level risk limit enforcement (daily loss caps, max concurrent positions).

**Scope excludes:** any decision about whether to trade.

---

## The upstream pipelines

Two pipelines feed the three-layer runtime system. They operate at development-time cadences (minutes to weeks), not runtime.

### Feature pipeline

Transforms raw data into registered, point-in-time-correct features and model-backed features. Upstream of everything. Detailed in `02_feature_pipeline.md`.

Seven stages: ingestion, audit, source admission, computation, model training, validation, registration.

Produces three registries: data sources, features, models. Each versioned, each queryable via API. Sources are admitted (or not) against a single universal bar — there is no quality ladder.

### Strategy pipeline

Transforms hypotheses into validated library strategies. Detailed across documents `01`, `04`, `06`, `07`, `08`.

Ten stages: hypothesis → mechanism articulation → specification → sanity-check backtest → walk-forward validation → CPCV and robustness → paper trading → promotion review → live with monitoring → retirement or revision.

Fed by the hypothesis system (document `06`), augmented by the auto-generator (document `07`), validated by the backtest harness (document `05`), constrained by the strategy class registry (document `03`) and strategy spec schema (document `04`).

---

## Component map

```
DATA SOURCES (external)
    ↓
┌───────────────────────────────────────────────┐
│ FEATURE PIPELINE                              │
│   Ingestion → Audit → Source Admission        │
│   Feature Computation → Model Training        │
│   Validation → Registration                   │
│                                               │
│   Produces: Data/Feature/Model Registries    │
└───────────────┬───────────────────────────────┘
                │
                │ (features consumed by everything downstream)
                │
    ┌───────────┴────────────┐
    │                        │
    ▼                        ▼
┌──────────────────┐   ┌─────────────────────────────┐
│ HYPOTHESIS       │   │ RUNTIME SYSTEM              │
│ SYSTEM           │   │                             │
│   Ingestion      │   │  ┌───────────────────────┐  │
│   Enrichment     │   │  │ STRATEGIC LAYER (LLM) │  │
│   Triage         │   │  │ - Pre-session brief   │  │
│   ↓              │   │  │ - Selection cycles    │  │
│ STRATEGY         │   │  │ - Position management │  │
│ PIPELINE         │   │  │ - Post-session review │  │
│   Spec           │   │  └──────────┬────────────┘  │
│   Auto-gen       │   │             │               │
│   Backtest       │   │             ▼               │
│   Validation     │   │  ┌───────────────────────┐  │
│   Paper          │   │  │ TACTICAL LAYER        │  │
│   Promotion      │   │  │ Strategy state        │  │
│   ↓              │   │  │ machines              │  │
│ LIBRARY          │──▶│  │ (deterministic)       │  │
│   (live specs)   │   │  └──────────┬────────────┘  │
│                  │   │             │               │
└──────────────────┘   │             ▼               │
                       │  ┌───────────────────────┐  │
                       │  │ EXECUTION LAYER       │  │
                       │  │ IB API integration    │  │
                       │  └──────────┬────────────┘  │
                       │             │               │
                       └─────────────┼───────────────┘
                                     │
                                     ▼
                                IB BROKER
```

---

## Data flow — a live trading day

**Pre-open (once):** Strategic layer produces pre-session brief. Reviews overnight data, scheduled events, sentiment, regime state, correlations. Drafts initial active playbook. Human review for first N weeks of live; auto-apply once statistics demonstrate selection quality.

**Market open:** Active strategies transition from DORMANT to ARMED. Each begins consuming the feature stream and monitoring its trigger conditions.

**Intra-session cycle (every 5–10 min):** Strategic layer re-evaluates. Current context compared against library; active set adjusted; open positions reviewed. Event triggers (price moves, scheduled releases, position thresholds) can force off-cycle reeval.

**Trigger fires:** Strategy in ARMED state detects its entry condition, moves to ENTERING, submits order via execution layer.

**Fill:** Strategy moves to IN_POSITION. Submits protective stop. Begins managing exits per its mechanical rules.

**Exit (strategy-initiated or LLM-initiated):** Strategy moves to EXITING. Closes position. Moves to DONE_FOR_SESSION if single-attempt, or back to ARMED if multi-attempt.

**Market close:** All strategies move to DORMANT.

**Post-close (once):** Strategic layer produces post-session review. Evaluates selection decisions, strategy performance vs envelope, proposes library updates (to the hypothesis queue, not the library itself).

---

## Governance model

Every state change that touches capital requires appropriate authority:

| Action | Authority required |
|---|---|
| New data source registered | Human sign-off on admission evidence |
| New feature registered for live | Human sign-off |
| New strategy promoted to `live` tier | Human sign-off, full validation packet |
| Strategy moved from `confirm_then_execute` to `auto_execute` | Human sign-off, minimum N successful live trades |
| Strategy auto-disabled by monitoring | Automatic; re-enable requires human + fresh validation |
| Strategy parameters changed within envelope | Version bump, harness re-run, no human required if within tested envelope |
| Strategy parameters changed outside envelope | Full validation re-run, human sign-off |
| LLM selection overridden by human | Logged; no approval needed |
| Risk envelope expanded | Human sign-off |
| Feature deprecated | Dependent strategies migrated first; human sign-off |

The human-in-loop horizon relaxes over time as the system demonstrates statistical evidence of correct behavior. It does not disappear.

---

## Tier model for runtime strategies

Each live library strategy runs at one of three tiers:

- **`proposal_only`** — logged for review, never traded. Used during initial library build-out and for strategies whose live behavior needs observation before capital is committed.
- **`confirm_then_execute`** — LLM proposes, human confirms via fast approval interface. Default for newly live strategies.
- **`auto_execute`** — LLM picks, tactical layer runs it. Only after minimum live trade count with envelope-consistent performance.

Tier is a spec field, operator-mutable without version bump, logged in ops audit trail.

---

## What the system is not

- **Not a high-frequency system.** Minimum cycle time is minutes. Signal-to-noise and reasoning depth are the edge, not reaction speed.
- **Not a black-box ML system.** Every decision is either mechanically determined (strategies) or structured-output-reasoned with rationale logged (LLM layer). No unexplained trades.
- **Not a bet-the-farm system.** Risk per trade is small, max positions bounded, daily/weekly loss caps enforced by the execution layer regardless of what any layer above decides.
- **Not autonomous.** Human governance at all judgment gates. The system runs within human-set rails.
- **Not optimized for capacity.** At account sizes above $100k–$500k this architecture would need rethinking. Starting at $10k.

---

## Document set

| # | Document | Status |
|---|---|---|
| 00 | Master architecture (this document) | draft |
| 01 | Strategy definition | draft |
| 02 | Feature pipeline spec | drafted previously |
| 03 | Strategy class registry spec | draft |
| 04 | Strategy spec schema | drafted previously |
| 05 | Backtest harness spec | draft |
| 06 | Hypothesis system spec | draft |
| 07 | Auto-generation spec | draft |
| 08 | Development pipeline | draft |
| 09 | Selection layer spec | draft |

Expected future additions: execution layer spec, live monitoring spec, governance process doc, infrastructure/ops spec.

---

## Build sequencing

Critical path, earliest to latest:

1. Feature pipeline Phase 1 (MES + ES OHLCV ingestion under the live/historical parity contract — paired Sierra Chart historical adapter and IBKR live adapter per source — vital-signs feature set per `02_feature_pipeline.md` "Feature inventory growth", registry API). Further features are added on demand from hypothesis specs, not speculatively pre-registered.
2. Strategy class registry + initial 3–5 classes
3. Backtest harness MVP
4. Strategy spec schema validator
5. One strategy end-to-end through the pipeline (proof of concept)
6. Hypothesis system MVP
7. Auto-generation service
8. Additional feature pipeline sources (VIX, economic calendar, options)
9. First model-backed feature (HMM regime)
10. Selection layer MVP
11. Paper trading integration
12. Live monitoring and alerting
13. Production readiness review
14. Live with single strategy at `confirm_then_execute` tier
15. Scale library and automation tier progressively

Items 1–5 are foundational. Everything else depends on them.

---

## Open cross-cutting questions

1. **Multi-instrument expansion path.** When do we add NQ or RTY, and what's the minimum feature/strategy work required?
2. **Capital scaling triggers.** At what realized-performance thresholds do we scale from $10k → $25k → $50k?
3. **Disaster recovery.** What happens if IB is down mid-session with open positions? If the feature pipeline stops mid-session? If the LLM API is unavailable at selection time?
4. **Compute cost budget.** What's the monthly LLM spend envelope, and how do we monitor against it?
5. **Security model.** API keys, account access, ops role separation. Not addressed anywhere yet.
6. **Regulatory posture.** Personal-account algorithmic trading has regulatory touchpoints worth understanding before scaling.

These cut across multiple documents and likely warrant their own operational spec.
