# Auto-Generation Spec

**Status:** Draft for review (Phase A + B implemented 2026-05-01; feature-stat injection landed 2026-05-02)
**Purpose:** Define the automated generation of strategy spec variants from promoted hypotheses. Auto-generation widens the development funnel without compromising rigor. It produces variants for early-stage evaluation; it does not produce live library strategies.

## Implementation status

| Section | Status | Code path |
|---|---|---|
| Hypothesis schema + loader | ✅ implemented (Phase A, 2026-05-01) — Pydantic model with mechanism, falsification, parameter_envelope, variant_budget (cap 15), gate_thresholds | `src/tradegy/auto_generation/hypothesis.py` |
| VariantRecord + JSONL append-only log | ✅ implemented (Phase A) | `src/tradegy/auto_generation/records.py` |
| HypothesisGenerator + VariantGenerator ABCs (with stubs) | ✅ implemented (Phase A) | `src/tradegy/auto_generation/generators.py` |
| AutoTestOrchestrator (sanity → walk-forward, multi-hypothesis correction, pre-registration enforcement) | ✅ implemented (Phase A) — Bonferroni-flavoured Sharpe lift; full DSR is open work | `src/tradegy/auto_generation/orchestrator.py` |
| AnthropicHypothesisGenerator (LLM ideation) | ✅ implemented (Phase B, 2026-05-01) — opus-4-7, adaptive thinking, prompt-cached registry block | `src/tradegy/auto_generation/anthropic_generators.py` |
| AnthropicVariantGenerator (LLM spec drafting) | ✅ implemented (Phase B) — prose-instructed JSON via `messages.create()` (the strict-output grammar compiler rejected the full-spec schema as too complex during the 2026-05-01 dry run); we Pydantic-validate on our side | same file |
| Cost reporting | ✅ implemented (Phase B) — post-call USD estimate from `response.usage`; non-blocking | `src/tradegy/auto_generation/cost.py` |
| `tradegy hypothesize` / `auto-vary` / `auto-test` / `hypothesis-list` CLI | ✅ implemented (Phase B) | `src/tradegy/cli.py` |
| Per-feature distribution stats injected into the LLM prompt | ✅ implemented (Phase C, 2026-05-02) — for each registered feature, the cached registry block carries (rows, min, max, p10, median, p90) computed from the live parquet. Anchors LLM threshold proposals inside the actual distribution. | `src/tradegy/auto_generation/feature_stats.py` |
| `tradegy refresh-feature-stats` CLI | ✅ implemented (Phase C) — pre-warms `data/feature_stats/<id>.json` from materialised features. `hypothesize` / `auto-vary` accept `--refresh-stats` to recompute on demand. | `src/tradegy/cli.py` |
| Embedding-based diversity check | ⚠️ Phase C-pending — content-hash dedup is the MVP placeholder | (Phase C) |
| Deflated Sharpe Ratio (López de Prado) | ⚠️ Phase C-pending — Bonferroni is the MVP correction | (Phase C) |
| Hypothesis triage / five-test scorer | ⚠️ Phase C-pending — schema fields exist, scorer not wired | (Phase C) |
| Holdout integration (auto-test path) | ⚠️ deferred — slot wired in orchestrator; the CLI's `--holdout-months` flow is the production path; auto-test should reuse it | (Phase C) |

---

## Design principles

1. **Funnel widener, not shortcut.** Auto-generation expands how many hypotheses can be tested early. It does not bypass any validation gate.
2. **Bounded by registries.** Auto-generated specs can only compose existing registered strategy classes, sizing classes, stops, exits, and condition evaluators. Novel logic requires code changes.
3. **Statistical integrity first.** Variant counts are constrained by what can be statistically validated, not by compute capacity.
4. **Mechanism from hypothesis, mechanics from generator.** The LLM generates mechanical expressions of a hypothesis; it does not invent hypotheses.
5. **Kill by default.** Generated variants are discarded unless they clear pre-specified gates.
6. **Human decides what advances.** Nothing auto-promotes past walk-forward. Every move to CPCV or paper trading is a human decision.

---

## Where auto-generation is allowed vs forbidden

**Allowed:**
- Stage 3 (Specification): LLM drafts spec variants from a promoted hypothesis
- Stage 4 (Sanity-check backtest): harness auto-runs all variants
- Stage 5 (Walk-forward validation): harness auto-runs survivors

**Forbidden:**
- Stage 1 (Hypothesis generation): hypotheses come from the hypothesis system, not the auto-generator
- Stage 6+ (CPCV, paper trading, promotion): human gates, period
- Live library inclusion: never automated
- Live trading decisions: never automated

---

## Variant generation process

Given a promoted hypothesis, the auto-generator produces N variants (see budget rules below). Each variant is a full strategy spec conforming to `04_strategy_spec_schema.md`.

### Input to the generator

- The hypothesis record (observation, mechanism, counterparty, falsification)
- Current strategy class registry (available classes with parameter schemas)
- Current feature registry (available features with coverage, cadence, availability latency, and revisability)
- Pre-registered variant budget and statistical constraints
- Previously-generated variants for this hypothesis (to ensure diversity)

### LLM task

```
Given this hypothesis and its mechanism, generate N variant strategy specs that
mechanically express the hypothesis using only registered classes and features.
Each variant should differ meaningfully from the others in one or more of:
- Trigger formulation (what precisely fires entry)
- Confirmation filter (what distinguishes valid vs invalid setups)
- Exit logic (target, stop, invalidation structure)
- Timeframe or window parameters
- Feature dependencies (which features the trigger/confirmation/exit reference)

Variants must NOT:
- Invent new strategy classes
- Reference features outside the registry
- Use parameters outside class-declared ranges
- Be near-duplicates of each other
```

Output: N structured spec drafts, each with pre-registration (what would confirm / refute the hypothesis in this specific expression).

### Variant validation (automated, pre-backtest)

For each generated variant:
- Schema compliance
- Registry resolution (every referenced class and feature exists)
- Parameter envelope compliance
- Revisability inheritance check (flags variants whose dependencies require bitemporal handling)
- Diversity check against sibling variants (embedding-based; near-duplicates collapsed)

Variants failing any check are discarded with logged reason.

---

## Variant budget

**Budget per hypothesis: 5–15 variants, hypothesis-dependent.**

The budget is **pre-specified per hypothesis** before any backtests run. Not chosen based on results.

Budget-setting rules:
- Simple hypothesis with one primary trigger type: 3–5 variants
- Hypothesis with multiple plausible confirmation approaches: 5–10 variants
- Hypothesis spanning multiple timeframes or context sub-cases: 10–15 variants
- Maximum hard cap: 15

**Why not more:**

- **Multiple-hypothesis correction.** At N variants, the Sharpe threshold needed to clear statistical significance grows with log(N). At 15 variants, required clearance is manageable; at 200+, it exceeds what realistic edges produce.
- **Finite data.** ES data has limited independent information content. Past a point, variants re-mine the same noise.
- **Hypothesis capacity.** A single hypothesis has maybe 10–50 meaningfully distinct mechanical expressions. Beyond that, variants are near-duplicates inflating count without adding information.
- **Review bottleneck.** 15 survivors from 15 variants is reviewable. 15 survivors from 2 million is a lottery with no distinguishable winner.

The budget is not a compute budget. It is a **statistical budget**.

---

## Multi-hypothesis correction

When N variants are tested and the best is selected, the reported performance is biased upward. Correction applied:

### Deflated Sharpe Ratio (preferred)

Uses López de Prado's formulation. Given N variants tested, their Sharpe ratios, and their correlations, computes the Sharpe a variant would need to achieve to reject the null "all variants are noise" at a specified confidence level.

### Bonferroni or Benjamini-Hochberg (simpler fallback)

If Deflated Sharpe computation is unavailable for a particular scenario, Bonferroni correction on the p-values or Benjamini-Hochberg for FDR control. More conservative than Deflated Sharpe but simpler.

### Reported metrics

Every variant evaluation reports:
- Raw Sharpe
- Deflated Sharpe (or correction-adjusted Sharpe)
- Number of variants tested for the hypothesis
- Whether the variant clears the corrected threshold

The variant that survives is the one that clears the corrected threshold, not the one with the highest raw Sharpe.

---

## Candidate pool

Variants that clear the pre-specified gates go into a **candidate pool**, not the library.

Candidate pool entries include:
- Generated spec (full schema)
- Parent hypothesis reference
- Backtest results (sanity-check + walk-forward)
- Corrected Sharpe and threshold-clearance status
- Variant siblings (other variants tested for same hypothesis)
- Holdout evaluation (see below)

Human review selects from the candidate pool which variants (typically 1–2 per hypothesis) advance to manual CPCV (Stage 6).

---

## Independent holdout

A reserved slice of historical data (default: most recent 6 months) that auto-generation never touches for any hypothesis.

Purpose: after variant selection on the non-holdout data, the winning variant is evaluated on the holdout as a final sanity check before human review. Performance collapse on holdout → hypothesis likely dies.

Holdout access is strictly controlled:
- Auto-generator never reads it
- Harness runs on holdout only after the variant is selected
- Holdout results are not used to re-rank variants
- Once a hypothesis's winner has been evaluated on holdout, that hypothesis cannot re-use the holdout with different variants (would contaminate)

Holdout refresh policy: rotate the 6-month window annually. Each hypothesis gets one shot at the holdout.

**Implementation (2026-05-01):** `tradegy walk-forward` and `tradegy cpcv` accept `--holdout-months N`. When set, the trailing N months are reserved from all walk-forward folds / CPCV paths; after the primary gate passes, a single backtest runs on the held-out window and is gated at `0.5× reference_sharpe` (avg OOS Sharpe for walk-forward; median CPCV Sharpe for cpcv). Failure exits with code 5 (walk-forward) or 6 (cpcv). The held-out window is point-in-time correct: no fold or path ever sees data inside the holdout.

---

## Gates

Pre-specified per hypothesis, logged before runs:

**Sanity-check gate (Stage 4):**
- Variant executes without errors
- Plausible trade count (within declared expected range)
- Raw Sharpe > 0 in-sample
- No lookahead audit failures

**Walk-forward gate (Stage 5):**
- Deflated Sharpe > threshold (default 0.8)
- Out-of-sample Sharpe within 50% of in-sample Sharpe
- Parameter sensitivity passes (no cliffs within ±20%)
- Positive after cost modeling

**Holdout gate:**
- Holdout Sharpe > 0.5× walk-forward Sharpe
- No anomalous trade patterns vs walk-forward

Failing any gate at any stage = discard the variant. Failure at holdout gate is especially telling — it indicates the variant cleared earlier gates partly through luck / mild overfitting.

---

## Variant tracking

Every generated variant gets logged regardless of outcome:

```yaml
variant_record:
  variant_id: "..."
  hypothesis_id: "..."
  generated_at: "..."
  generator_version: "..."
  generator_llm_model: "..."
  generation_seed: "..."    # for reproducibility
  budget_used: 8            # variants generated for this hypothesis
  budget_cap: 10
  spec_hash: "..."
  spec_content: {...}
  gate_results:
    sanity_check: passed
    walk_forward: failed
    holdout: not_run
  stats:
    raw_sharpe: ...
    deflated_sharpe: ...
    corrected_threshold: ...
    ...
  outcome: "discarded_at_walk_forward"
  sibling_variants: [...]
```

Persisted forever. Audit trail: "for hypothesis X, how many variants did we test, what did we find, what did we pick."

---

## Post-hoc rules (explicit prohibitions)

Things that are forbidden after results are seen:

- **Adjusting the variant budget.** Fixed before generation.
- **Adjusting the gate thresholds.** Fixed before generation.
- **Generating more variants because the initial batch underperformed.** Requires a new hypothesis with a different mechanism, or closing this hypothesis as unprofitable.
- **Selecting the variant with the highest raw Sharpe when a different variant clears the corrected threshold.** The corrected threshold is the criterion.
- **Running the holdout multiple times with different variant sets.** One shot per hypothesis.

Violating any of these corrupts the statistical basis. All gate thresholds and budgets are recorded at hypothesis promotion time and immutable.

---

## Relationship to manual strategy development

Auto-generation does not replace manual strategy development. Both coexist:

- **Manual development:** a human (possibly with LLM assistance) authors a single well-considered spec for a hypothesis, runs it through the full pipeline individually. Appropriate for hypotheses with a clearly-preferred mechanical expression.
- **Auto-generation:** multiple variants for a hypothesis with multiple plausible mechanical expressions. Appropriate when the right mechanical framing isn't obvious.

The choice between manual and auto is a decision at hypothesis promotion time. Logged in the development ticket.

---

## LLM prompt structure

The generator uses a structured prompt template:

```
CONTEXT:
- You are generating mechanical strategy spec variants for a promoted hypothesis.
- Variants will be statistically validated. Multiple-hypothesis correction applies.
- You may only use registered classes and features.

HYPOTHESIS:
{hypothesis observation, mechanism, counterparty, falsification}

AVAILABLE STRATEGY CLASSES:
{class registry filtered to classes plausibly relevant to this hypothesis}

AVAILABLE FEATURES:
{feature registry filtered to features with compatible cadence, availability latency, and coverage for the hypothesis}

CONSTRAINTS:
- Budget: {N} variants
- Statistical correction: Deflated Sharpe with N={N}
- Pre-registered gates: {gate specs}

PRIOR VARIANTS:
{if regenerating, prior variants so far}

YOUR TASK:
Produce {N} mechanical spec variants, each a JSON object conforming to the
strategy spec schema. Each should differ meaningfully in trigger, confirmation,
or exit logic.

For each variant:
1. The spec itself
2. A one-paragraph rationale: why does this mechanical expression test the hypothesis
3. A pre-registered prediction: what would this variant's walk-forward Sharpe look like if the hypothesis is true vs false

Return JSON array of {N} variants.
```

Output validated against schema before entering evaluation pipeline.

---

## Cost management

LLM costs for auto-generation scale with:
- Number of hypotheses promoted × budget per hypothesis × prompt+output token count

Rough estimate at 30 hypotheses/quarter × 10 variants/hypothesis × large LLM: monthly four-figure token spend. Not large, but monitor.

Compute costs for harness runs:
- 30 hypotheses × 10 variants × full walk-forward = 300 walk-forward runs/quarter

Also manageable but meaningful. Scheduling policy: auto-generation runs overnight in batches, not on-demand.

---

## Open design decisions

1. **LLM model selection.** Quality of variant generation matters. Use the strongest available model for this task even if more expensive per call — generation quality compounds through the pipeline.

2. **Diversity enforcement.** Embedding-based diversity check is one approach. Alternative: explicitly enumerate variant dimensions (timeframe, confirmation type, exit type) and require variants to differ on at least one. Simpler, more controllable. Leaning explicit dimensions.

3. **Regeneration on poor batch.** If all generated variants fail sanity check, is that the hypothesis dying or the generator failing? Propose: if ≥80% of variants fail sanity check, flag for human review rather than auto-killing the hypothesis.

4. **Parameter optimization within variants.** Does each variant come with pre-chosen parameter values, or is the variant a template that the walk-forward tunes within-envelope? Leaning pre-chosen (simpler, avoids compounding the multiple-testing problem with within-variant tuning). Optional: allow variants to declare "this parameter should be optimized over the declared envelope" and add tuning to the multiple-hypothesis correction.

5. **Cross-hypothesis learning.** Should the generator learn from past hypotheses' results? E.g., "hypotheses about ORB behavior have historically favored variants with volume confirmation over delta confirmation." Potentially useful but introduces subtle leakage and overfitting to recent experience. Probably not in v1.
