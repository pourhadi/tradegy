# Development Pipeline

**Status:** Draft for review
**Purpose:** Define the end-to-end process by which a promoted hypothesis becomes a live library strategy, lives in the library, and eventually retires. The pipeline is deliberately long and gate-heavy. Skipping stages is how libraries get filled with overfit noise. The process is the product.

---

## Design principles

1. **Every stage has explicit inputs, outputs, and gates.** No implicit transitions. No silent promotions.
2. **Kill early, kill cheap.** The first gates are the cheapest to run and the most likely to kill a candidate. Expensive stages (CPCV, paper trading) only see candidates that have cleared the inexpensive filters.
3. **Gates are pre-specified, not post-hoc.** Thresholds set before runs, not adjusted based on results.
4. **Human judgment at high-stakes transitions.** Mechanical stages automated; promotion and retirement require human sign-off.
5. **Falsification-first evidence.** Each stage asks "does this refute the hypothesis" as much as "does this confirm it."
6. **Audit trail is non-negotiable.** Every decision, every transition, every sign-off logged with rationale.

---

## The ten stages

```
Hypothesis (from hypothesis system)
    │
    ▼
Stage 1 — Hypothesis intake
    │
    ▼
Stage 2 — Mechanism articulation
    │
    ▼
Stage 3 — Specification
    │
    ▼
Stage 4 — Sanity-check backtest
    │
    ▼
Stage 5 — Walk-forward validation
    │
    ▼
Stage 6 — CPCV and robustness
    │
    ▼
Stage 7 — Paper trading
    │
    ▼
Stage 8 — Promotion review
    │
    ▼
Stage 9 — Live with monitoring
    │
    ▼
Stage 10 — Retirement or revision
```

---

## Stage 1: Hypothesis intake

**Input:** Hypothesis promoted from the hypothesis system (status: `triaged_candidate`).

**Activities:**
- Development ticket created, linked bi-directionally to the hypothesis record
- Pre-registration document template instantiated from the mechanism articulation
- Initial data-requirement spec assembled
- Decision: manual development vs auto-generation (logged in ticket)

**Outputs:** Development ticket in `pending_mechanism_articulation` status; pre-registration template ready for completion.

**Gate:** Hypothesis has cleared triage with sufficient score; data requirements are feasible; no blocking duplicates with in-development candidates.

**Expected kill rate at this stage:** ~5%. Most issues would have been caught at triage.

---

## Stage 2: Mechanism articulation

**Input:** Development ticket from Stage 1.

**Activities:**

- **Mechanism stress-test.** Examine mechanism against current market structure. Does it still hold? Who benefits, who loses?
- **Counterparty identification.** Name the entity taking the other side. Structural (mandate/hedging/behavioral) vs accidental (learnable mistake). Structural counterparties support durable edges.
- **Arbitrage-resistance test.** Why hasn't this been traded away? Capacity limit, infrastructure, mandate, behavioral stickiness acceptable. "No one has noticed" unacceptable.
- **Pre-registration.** Write down, before any backtests run: what results would confirm the hypothesis, what would refute it, what would be ambiguous. Committed to version control.

**Outputs:** Completed mechanism document, pre-registration document, data-requirement spec, updated development ticket.

**Gate:** Skeptical reviewer (human or LLM acting as skeptic) agrees mechanism is worth development time. Pre-registration committed.

**Expected kill rate:** 40–60%. This is where most hypotheses should die. If kill rate here is low, Stage 1 filtering is too loose.

---

## Stage 3: Specification

**Input:** Mechanism document and pre-registration.

**Activities:**

- **Draft spec** conforming to strategy spec schema. Entry, sizing, stops, exits, context conditions. Derive mechanics directly from mechanism — tight coupling between mechanism story and mechanical expression.
- **Identify feature dependencies.** Every feature referenced must be in the feature registry or queued for addition.
- **Pick initial parameter values** informed by mechanism and prior art, not by "what makes backtest look best."
- **Pre-specify parameter envelope** intended for testing. Written before first backtest. Prevents post-hoc rationalization.
- **Author `context_conditions`** — draft based on expected conditions, to be refined from backtest evidence in later stages.
- **If auto-generation path:** the auto-generator produces N variant specs per the rules in `07_auto_generation.md`.

**Outputs:** One spec (manual path) or N variant specs (auto path), each conforming to schema, all ready for backtest.

**Gate:** Spec passes schema validation; all registered references resolve; internal consistency between mechanism and mechanics.

**Expected kill rate:** 5–10%. Most rejections here are "mechanism doesn't cleanly translate to mechanical rules" — important signal that the mechanism was vaguer than it seemed.

---

## Stage 4: Sanity-check backtest

**Input:** Spec(s) from Stage 3.

**Activities:**
- Run spec across a subset of in-sample data (e.g., 2 years)
- Initial parameters, single configuration
- Visual review of actual trades — do they look like the mechanism predicts?
- Statistics plausibility — win rate in reasonable range, trade count roughly as expected, holding times within spec

**Outputs:** Sanity-check report. Trade log. Visual samples.

**Gate:**
- Strategy behaves mechanically as spec says
- Produces plausible trade count (not 3 trades in 2 years, not 5 per day if spec says 1)
- Shows nonzero edge in the right direction
- No signs of obvious bugs (timezone issues, feature leakage, spec misinterpretation)

**Note on suspicious results:** A spectacularly-positive sanity check is a red flag, not a green light. Strong results this early usually indicate leakage or overfit assumptions. Investigate before proceeding.

**Expected kill rate:** 20–30%. Usually kills for "mechanism doesn't produce the expected pattern" or "obvious bug discovered."

---

## Stage 5: Walk-forward validation

**Input:** Sanity-check-passing spec(s).

**Activities:**
- Walk-forward across full history with rolling windows (e.g., 3yr train, 1yr test, roll annually)
- Parameter sensitivity sweep (±20% perturbation grid)
- Regime stratification (performance by trending/range/high-vol/low-vol regimes)
- Baseline comparison (buy-and-hold, random-entry-matched-holding, simple regime rule)
- If auto-generation path: multi-hypothesis correction across variants

**Outputs:** Walk-forward report with per-window Sharpe, aggregate out-of-sample Sharpe, sensitivity surface, regime-stratified stats, baseline comparisons. If auto-generation path, selection of variant(s) to advance.

**Gate:**
- Out-of-sample Sharpe meaningfully positive (threshold pre-specified, commonly > 0.8 for deflated Sharpe)
- Out-of-sample within ~30–50% of in-sample (greater divergence = overfit)
- Parameter sensitivity passes (no cliffs within ±20%)
- Beats all baselines meaningfully
- Regime stratification reveals clear context envelope (not "works everywhere")

**Expected kill rate:** 30–50%. Common causes: overfit, parameter sensitivity cliffs, fails to beat random-entry baseline (strategy is just holding-time exposure).

---

## Stage 6: CPCV and robustness

**Input:** Walk-forward-passing spec.

**Activities:**
- Combinatorial Purged Cross-Validation (multiple paths, purging, embargo)
- Realistic cost modeling (commissions, slippage from modeled spreads, margin cost)
- Stress-period replay (March 2020, Feb 2018, Aug 2015, Q4 2022)
- Capacity estimate
- Refinement of `context_conditions` from accumulated evidence (LLM drafts, human reviews)
- Retirement criteria authored from mechanism — what live signals would invalidate the thesis

**Outputs:** Complete `validation_record` and `backtest_evidence` sections of the spec. Context conditions finalized. Retirement criteria in place.

**Gate:**
- CPCV median Sharpe above threshold (pre-specified)
- Percentage of CPCV paths negative below threshold (commonly <20%)
- Net-of-cost performance positive (gross-positive/net-negative = dead)
- Stress behavior understood and acceptable
- Capacity sufficient for MES trading

**Expected kill rate:** 20–30%. Common causes: honest cost modeling kills marginal edges; stress behavior reveals hidden tail risk; CPCV reveals backtest was hanging on a few lucky periods.

---

## Stage 7: Paper trading

**Input:** CPCV-passing spec with full validation evidence.

**Activities:**
- Deploy to paper broker with real-time data and simulated execution
- Real timing, real slippage as modeled by broker
- Minimum trade count before evaluating (ideally 30+; at our cadences, 2–3 months)
- Interact with selection layer in parallel — this is the first time selection-layer quality matters
- Track divergence from backtest: are fills matching modeled fills, is trade cadence matching, is holding-time distribution consistent
- Tier starts as `confirm_then_execute` — human approves each trade via fast UI

**Outputs:** Paper trading record with realized stats, divergence analysis, selection-layer interaction notes.

**Gate:**
- Sufficient trade count for statistical meaning
- Paper Sharpe within ~40% of backtest Sharpe (some underperformance expected from execution realities)
- No pathological divergences
- Selection layer's activation decisions align with declared context conditions
- No material live-only issues (data latency, feed quality, broker weirdness) unaddressed

**Expected kill rate:** 10–20%. Most survivors to this stage make it through, but execution realities kill some.

---

## Stage 8: Promotion review

**Input:** Paper-trading-passing spec.

**Activities:**
- **Assemble promotion packet:** mechanism doc, pre-registration, validation evidence, paper trading record, retirement criteria, declared library fit rationale.
- **Check library fit:** correlation with existing live strategies, whether addition improves portfolio on the margin vs adds redundancy.
- **Confirm retirement criteria.** Must be written before going live, not after. Includes both quantitative (auto-disable thresholds) and qualitative (structural conditions that would invalidate the mechanism) triggers.
- **Human sign-off(s).** Deliberate, documented, reasoning recorded. Not a rubber stamp.
- **Tier decision.** New live strategies start at `confirm_then_execute`. Graduation to `auto_execute` is a separate later decision after demonstrated live performance (minimum N live trades, envelope-consistent performance).

**Outputs:** Signed promotion packet. Spec updated to `status: live`, `operational.tier: confirm_then_execute`, `operational.live_since: {date}`.

**Gate:** Signed promotion packet. All retirement criteria declared. Library-fit evaluation complete.

**Expected kill rate:** 5–10%. Mostly rejections for poor library fit or insufficient paper trading evidence. Rarely the mechanism itself at this point.

---

## Stage 9: Live with monitoring

**Input:** Live-tier spec.

**Activities (continuous):**
- Automated performance tracking: realized vs expected Sharpe, drawdown vs envelope, trade cadence, holding-time distribution, hit rate
- Envelope breach detection: alert when realized performance exits expected band
- Automated retirement-trigger monitoring per the spec's `retirement_criteria.quantitative_triggers`
- Auto-disable if CRITICAL thresholds hit (strategy pulled from live, requires human re-enable)
- Flag-for-review on SOFT thresholds

**Periodic activities:**
- **Quarterly revalidation:** rerun CPCV and walk-forward on most recent data. Parameters that were optimal in 2024 may not be in 2026.
- **Annual mechanism re-examination:** explicitly revisit whether the causal story is still operative. Market structure evolves.
- **Tier progression review:** after sufficient trades and evidence, consider graduating from `confirm_then_execute` to `auto_execute`.

**Outputs:** Live performance records. Revalidation reports. Tier changes. Retirement flags.

**Transitions out:**
- To Stage 10 (retirement) via quantitative trigger or qualitative decision
- To version revision (back to an earlier stage of a new MAJOR version) if mechanism needs adjustment

---

## Stage 10: Retirement or revision

**Input:** Live-tier strategy with retirement trigger fired or revision decision made.

**Retirement:**
- Strategy's `enabled` flag set to false
- Removed from selection-layer candidate set
- Open positions closed per spec's session-end or emergency-flatten rules
- Spec moves to `status: retired`
- Historical record preserved; spec stays versioned and queryable
- Retirement reason logged

**Re-enablement:** Retired strategies are not quietly re-enabled. Re-enablement requires fresh validation against recent data, a new promotion packet, and explicit human sign-off — same gates as a new strategy.

**Revision:** If the underlying idea remains valid but the mechanics need adjustment:
- MAJOR version bump on the spec
- Changes re-enter the pipeline at an appropriate stage (usually Stage 3 or Stage 5, depending on scope of change)
- Old version goes to `status: retired`; new version progresses through its own validation

**Triggers for retirement vs revision:**
- Quantitative auto-disable: retirement (re-enable requires fresh work)
- Mechanism fails (structural change): retirement
- Parameters have drifted out of validated envelope: revision
- Better variant identified with same underlying mechanism: revision (old retired, new promoted)

---

## Kill-rate summary

| Stage | Expected kill rate | Primary reason |
|---|---|---|
| 1 — Intake | ~5% | Cleared at triage already |
| 2 — Mechanism | 40–60% | Weak mechanism; no structural counterparty |
| 3 — Specification | 5–10% | Mechanism doesn't cleanly mechanize |
| 4 — Sanity check | 20–30% | Bugs, no edge, unexpected behavior |
| 5 — Walk-forward | 30–50% | Overfit, sensitivity cliffs, fails baselines |
| 6 — CPCV / robustness | 20–30% | Honest costs kill edge; stress reveals tails |
| 7 — Paper trading | 10–20% | Execution realities; divergence from backtest |
| 8 — Promotion | 5–10% | Poor library fit |

**Cumulative survival from promoted hypothesis to live library: roughly 5–15%.** Most hypotheses that enter the pipeline don't exit as live strategies. That's the process working, not failing.

---

## Pipeline-level monitoring

Metrics tracked across all in-flight development:

- Stage dwell time (how long is each stage taking?)
- Kill rate by stage (are the rates roughly as expected?)
- Hit rate from promoted hypothesis to live strategy (should be 5–15%)
- Hit rate by source (from `06_hypothesis_system.md`)
- Time to live (hypothesis promotion → live) — likely 3–6 months minimum given paper trading duration

Quarterly process review examines these metrics. Outliers (stages consistently killing too much, too little, too slowly) trigger process tuning.

---

## Human-in-loop touchpoints

Mandatory human review:

- Stage 2: mechanism document approval
- Stage 3: spec review (for manual path; variant selection from auto-gen for auto path)
- Stage 5: interpretation of walk-forward and sensitivity results
- Stage 6: CPCV interpretation; `context_conditions` review; retirement criteria authorship
- Stage 7: ongoing review of paper trading, each trade during initial `confirm_then_execute` period
- Stage 8: promotion packet sign-off
- Stage 9: tier progression decisions; quarterly revalidation review; annual mechanism review; soft-threshold flag response
- Stage 10: retirement decision; revision scope decision

LLM-assistable activities (LLM drafts, human reviews):

- Stage 2: mechanism articulation, counterparty identification
- Stage 3: spec drafting (especially auto-generation)
- Stage 4: anomaly interpretation in sanity-check results
- Stage 6: `context_conditions` drafting
- Stage 9: monitoring report synthesis; post-session reviews feeding back to hypothesis system

---

## What this pipeline does not do

- **It does not guarantee successful strategies.** It only guarantees that strategies entering live have passed defined gates. Gates can be wrong. Markets can shift.
- **It does not eliminate human judgment.** It structures where judgment is applied, not whether.
- **It does not adapt to markets within a strategy's lifetime.** Adaptation happens through governance — revision, retirement, new strategies — not within a live spec.
- **It does not compress.** Fast-tracking a strategy to live based on "it looks great in backtest" is how libraries accumulate noise. Hold the line on gates.

---

## The meta-point

A library of 6 strategies through this full pipeline is more valuable than a library of 30 strategies through a compressed pipeline. The selection layer's performance is bounded by library quality. A mediocre library produces mediocre trading regardless of how good the selection is.

The pipeline is slow on purpose. The slowness is what distinguishes real edges from artifacts. Process discipline is the primary long-term edge of this system.
