# Hypothesis System Spec

**Status:** Draft for review (scanner Phase 1 — kill-record + market-structure observer — landed 2026-05-02)
**Purpose:** Define the pipeline that produces, stores, triages, and promotes trading hypotheses. The hypothesis system is upstream of strategy development. It does not generate strategies — it generates the candidate ideas that the strategy pipeline then develops, or kills.

## Implementation status

| Section | Status | Code path |
|---|---|---|
| Hypothesis schema + YAML loader | ✅ implemented | `src/tradegy/auto_generation/hypothesis.py` |
| LLM hypothesis generator (`tradegy hypothesize`) | ✅ implemented (Phase B) | `src/tradegy/auto_generation/anthropic_generators.py` |
| Kill-record injection into LLM prompt | ✅ implemented (scanner Phase 1, 2026-05-02) — every hypothesis with `status: killed`/`retired` *or* whose variant log shows zero survivors is rendered as a "do not propose mechanistic near-duplicates" block in the hypothesis-generator system prompt. | `src/tradegy/auto_generation/kill_log.py` |
| In-data market-structure observer | ✅ implemented (scanner Phase 1, 2026-05-02) — `tradegy market-scan` computes recent-vs-baseline observations on realized vol, overnight gap magnitude, session-position concentration of largest 1m moves, and session volume; the most-recent snapshot is rendered as a "current market-structure observations" block in the hypothesis prompt. **Note:** this is the implemented *in-data* observer (Polars over the materialised parquets), distinct from the planned *external* market-structure monitor described under "Ingestion sources" below (CME rule changes, SEC filings, index-methodology changes — those still require a vendor feed and are not implemented). | `src/tradegy/auto_generation/market_scan.py` |
| Literature / event-calendar / anomaly / post-session / human-submission scanners | ⚠️ specced; not yet implemented | (none) |
| Five-test scorer (automated triage) | ⚠️ schema fields exist; scorer not wired | (Phase C-pending) |
| Embedding-based duplicate detection | ⚠️ content-hash dedup is the MVP placeholder | (Phase C-pending) |

---

## Design principles

1. **Source-aware skepticism.** Every hypothesis is tagged by source. Source determines default skepticism. Market-structure reasoning gets benefit of the doubt; data-mined patterns do not.

2. **Mechanism-required.** A hypothesis without a proposed mechanism is not a hypothesis; it's a pattern observation. Promotion requires articulated mechanism.

3. **Falsifiability-required.** Hypotheses that cannot state what would refute them do not proceed. Unfalsifiable ideas waste development cycles.

4. **Demand-filtered.** Hypotheses are cheap. Development is expensive. The system filters aggressively between generation and development.

5. **Human-gated promotion.** Automated ingestion, automated enrichment, automated scoring — but promotion to the development pipeline is a human decision, always.

6. **Feedback-driven.** The system tracks which sources produce usable hypotheses and adjusts effort allocation accordingly.

---

## Sources ranked by trustworthiness

1. **Market structure reasoning.** Deductive derivation from how markets actually work. Who trades, why, under what constraints, what patterns must therefore exist. Strongest source.
2. **Practitioner canon.** Well-documented strategies that experienced traders have used for decades. ORB, VWAP reversion, failed auction fades, gap behavior.
3. **Academic literature.** Market microstructure, behavioral finance, factor research. Quality varies — filter for mechanism and replication.
4. **Behavioral regularities.** Documented human trading behaviors — loss aversion, round-number magnetism, post-news overreaction.
5. **Own market observation.** Patterns noticed from watching markets. Valuable but dangerous — held to higher backtest standards.
6. **LLM-assisted synthesis.** LLM integrating context across sources. Useful for translating intuition, not as primary source.
7. **Data mining.** Statistical scans across tick data. Weakest source. Mechanism story required *after* pattern discovery, validated on untouched data.

---

## Pipeline architecture

```
┌─────────────────────────────────────────────────────────┐
│ INGESTION LAYER                                         │
│  - Literature scanner                                   │
│  - Market-structure monitor                             │
│  - Event calendar scanner                               │
│  - Anomaly detector (own data)                          │
│  - Post-session miner                                   │
│  - Human submission interface                           │
└───────────────────┬─────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────┐
│ HYPOTHESIS QUEUE (DB)                                   │
│  - Raw → enriched → triaged → candidate states          │
└───────────────────┬─────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────┐
│ ENRICHMENT LAYER (automated)                            │
│  - LLM mechanism articulation                           │
│  - Duplicate detection                                  │
│  - Five-test scoring                                    │
│  - Data feasibility check                               │
└───────────────────┬─────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────┐
│ TRIAGE (human, weekly)                                  │
│  - Promote / kill / dormant / merge                     │
└───────────────────┬─────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────┐
│ DEVELOPMENT HANDOFF                                     │
│  - Feeds strategy pipeline Stage 1                      │
└─────────────────────────────────────────────────────────┘
```

---

## Ingestion sources

### Literature scanner

Scheduled job. Scrapes from:
- SSRN (market microstructure, quant finance)
- arXiv q-fin
- Fed and other central bank working papers
- Selected practitioner blogs (curated allowlist)

For each new item:
- LLM produces structured summary: claimed effect, proposed mechanism, market/timeframe, data requirements
- Filters out low-signal sources (heuristic: too broad, no mechanism, non-tradeable timeframe)
- Writes to queue as `raw` status with source = `literature`

Cadence: daily scan, weekly digest to triage.

### Market-structure monitor

Tracks announcements affecting market structure:
- CME rule changes, margin changes, product launches
- Exchange notices
- SEC filings with market-structure implications
- Index methodology changes (S&P, MSCI)

Each change triggers a hypothesis prompt to an LLM: "does this create a new regularity worth investigating?" LLM output writes to queue.

Cadence: real-time on feed, triaged weekly.

### Event calendar scanner

Maintains forward calendar of scheduled events. Identifies:
- Events not currently covered by any library strategy's context conditions
- Event types with historical volatility signatures worth investigating

Output: hypothesis candidates targeting uncovered event windows.

Cadence: monthly.

### Anomaly detector

Runs nightly over recent ES session data. Statistical scans for:
- Unusual conditional return distributions
- Non-random patterns in time windows
- Regime-shift signatures
- Feature-pair correlation breakdowns

Outputs *candidates*, not conclusions. Each candidate is written to queue as `raw` with source = `data_mining`, explicitly flagged for elevated skepticism.

Cadence: nightly.

### Post-session miner

After each live/paper session, LLM reviews session log and asks:
- Which library strategies fit today, which didn't?
- What patterns appeared that no strategy captured?
- Were there periods with no active strategy where an edge looked available?

Output: hypotheses targeting gaps in library coverage.

Cadence: daily post-close.

### Human submission interface

CLI and/or simple web form. Required fields:
- Observation
- Proposed mechanism
- Source (book, paper, conversation, intuition, etc.)
- Data requirements (what we'd need to test this)

Low friction, structured output. Writes to queue as `raw` with source = `human_submission`.

Cadence: ad-hoc.

---

## Hypothesis queue schema

```sql
TABLE hypothesis (
  id                           UUID PRIMARY KEY,
  created_date                 TIMESTAMP NOT NULL,
  created_by                   TEXT,
  source                       ENUM(
                                 'market_structure_reasoning',
                                 'practitioner_canon',
                                 'academic_literature',
                                 'behavioral_regularity',
                                 'market_observation',
                                 'llm_synthesis',
                                 'data_mining',
                                 'post_session_mining',
                                 'human_submission'
                               ),
  source_detail                TEXT,  -- URL, paper citation, conversation reference

  -- The hypothesis itself
  observation                  TEXT NOT NULL,
  proposed_mechanism           TEXT,
  proposed_counterparty        TEXT,
  market_structure_dependencies TEXT,
  falsification_condition      TEXT,

  -- Feasibility
  data_requirements            JSONB,   -- feature IDs, source IDs
  data_feasibility_status      ENUM('feasible', 'partially_feasible', 'infeasible', 'unknown'),
  data_feasibility_notes       TEXT,

  -- Scoring (filled by enrichment)
  five_test_scores             JSONB,   -- {counterparty, arbitrage, applicability, testability, falsifiability}
  five_test_total              INTEGER,
  enrichment_notes             TEXT,
  enrichment_version           TEXT,

  -- Relationships
  related_hypothesis_ids       UUID[],
  related_strategy_ids         TEXT[],
  parent_hypothesis_id         UUID,   -- if variant of another

  -- Lifecycle
  status                       ENUM(
                                 'raw',
                                 'enriched',
                                 'triaged_candidate',
                                 'in_development',
                                 'promoted',       -- became a live library strategy
                                 'killed',
                                 'dormant',        -- not ready yet, revisit later
                                 'merged'          -- merged into another hypothesis
                               ),
  status_reason                TEXT,
  status_changed_date          TIMESTAMP,
  status_changed_by            TEXT,

  -- Audit
  last_reviewed_date           TIMESTAMP,
  review_history               JSONB   -- log of status changes with timestamps + actors
);
```

---

## Enrichment layer

Runs automatically on every `raw` hypothesis, producing an `enriched` version.

### Mechanism articulation (LLM)

For hypotheses with weak or missing mechanism fields (common for data-mined candidates), an LLM pass attempts to articulate the mechanism. Uses structured prompting:

```
Given this hypothesis, propose:
- The specific inefficiency being exploited
- Who is on the losing side of the trade and why
- What market-structural feature creates the opportunity
- Why this hasn't been arbitraged away
```

Output is *drafted evidence*, not decision. Flagged as LLM-generated for reviewer awareness.

### Duplicate detection

Embedding-based similarity search:
- Embed the observation + mechanism fields
- Compare against all existing queue entries and all live library strategies
- Similarity above threshold → flag for merge review

Prevents queue bloat from near-duplicates and catches rediscovery of existing library edges.

### Five-test scorer

LLM evaluates hypothesis against five pre-candidate tests. Each scored 0–3:

1. **Counterparty:** who's on the other side, and why are they willing to be there?
   - 0: no counterparty identified
   - 1: vague counterparty ("retail traders")
   - 2: specific counterparty with weak rationale
   - 3: specific counterparty with structural rationale

2. **Arbitrage resistance:** why hasn't this been traded away?
   - 0: no explanation
   - 1: "no one has noticed" (not acceptable)
   - 2: small edge or capacity limit
   - 3: clear structural barrier (infrastructure, mandate, behavioral stubbornness)

3. **Current-market applicability:** does the mechanism still apply today?
   - 0: mechanism is from a dead market structure
   - 1: mechanism's applicability is uncertain
   - 2: mechanism likely still applies with some adaptation
   - 3: mechanism is clearly current

4. **Testability:** can we test this with our data?
   - 0: requires data we don't have and can't get
   - 1: requires data we don't have but could acquire
   - 2: mostly testable with current data
   - 3: fully testable with current data

5. **Falsifiability:** can we state what would refute it?
   - 0: no falsification condition articulated
   - 1: vague falsification
   - 2: specific but hard-to-measure
   - 3: specific and measurable

Total score: 0–15. Scores feed triage priority but don't auto-decide.

### Data feasibility check

Automated: does the data required to test this hypothesis exist in our admitted sources / feature catalog with sufficient coverage, cadence, and availability latency for the test?

Output: `feasible | partially_feasible | infeasible | unknown`, with notes on what would need to be added.

---

## Triage (human, weekly)

Weekly session reviews enriched hypotheses in priority order. Priority driven by:
- Five-test total score (high first)
- Data feasibility (feasible first)
- Recency (newer first, to avoid queue stagnation)
- Source (higher-trust sources first)

For each reviewed hypothesis, decide:

- **Promote to candidate** → `triaged_candidate` status, enters development pipeline Stage 1
- **Kill** → `killed` status with reason recorded
- **Dormant** → `dormant` status with specific unblocker noted ("revisit when we have order-book data")
- **Merge** → `merged` into another hypothesis
- **Request more enrichment** → send back through enrichment with specific question

Triage is time-boxed: N hours weekly, hard cap on number of hypotheses reviewed. If queue grows faster than triage can process, ingestion sources are tuned down rather than triage stretched.

---

## Development handoff

When a hypothesis is promoted to `triaged_candidate`, the system creates:

1. A development ticket referencing the hypothesis
2. A pre-registration document template pre-filled from the mechanism articulation
3. An initial data-requirement spec
4. A link in both directions: hypothesis → development ticket, development ticket → hypothesis

The hypothesis stays in `triaged_candidate` status until the development process resolves — at which point it becomes `promoted` (became a live library strategy), `killed` (died in development), or `dormant` (paused pending some external change).

---

## Feedback loops

Two required loops feed back into the hypothesis system:

### Hit-rate tracking by source

For every hypothesis ever promoted to development, track the eventual outcome. Aggregate by source:

```
source                         total_promoted  promoted_to_library  hit_rate
market_structure_reasoning     ...             ...                  ...
practitioner_canon             ...             ...                  ...
...
data_mining                    ...             ...                  ...
```

Informs effort allocation. If `data_mining` hit rate is 2% and `market_structure_reasoning` is 40%, reduce effort on anomaly detector, invest in structure-reasoning prompts.

### Kill-reason tracking

For every killed hypothesis (or killed library candidate downstream), track the stage of death:

- Killed at triage (low scores, duplicates, infeasible)
- Killed at mechanism articulation
- Killed at sanity-check backtest
- Killed at walk-forward
- Killed at CPCV
- Killed at paper trading

Different death patterns imply different process problems:
- Lots of mechanism-articulation deaths → ingestion filtering is too loose
- Lots of CPCV deaths → earlier gates aren't catching overfitting
- Lots of paper-trading deaths → backtest fidelity or cost modeling is off

Feeds quarterly process review.

---

## Monitoring and health

System-level metrics tracked per quarter:

- Queue size, distribution by status
- Ingestion volume per source
- Enrichment latency
- Triage throughput vs ingestion volume
- Kill rate by stage
- Hit rate by source
- Time from ingestion to triage
- Time from promotion to library inclusion (or kill)

If queue is growing unboundedly, either triage capacity is insufficient or ingestion filters are too loose. If hit rate by source drops below a floor, investigate.

---

## Named hypotheses under investigation

The full hypothesis DB schema (above) is the durable home for this. Until the queue + UI is built, this table is the working ledger for hypotheses currently in flight or recently killed in the strategy-class pipeline (downstream of triage, since all entries here have already been promoted to development).

Each row records the kill stage and reason per the kill-reason taxonomy in §Kill-reason tracking.

| ID | Mechanism (one line) | Strategy class / spec id | Status | Stage of death | Kill reason / current state |
|---|---|---|---|---|---|
| H2 | VWAP fade gated by realized-vol mid-band + time-of-session window — gates fix the failure modes of the un-gated `mes_vwap_reversion` (regime-symmetric firing, last-30-min entries with no time to revert). | `vwap_reversion` / `mes_vwap_reversion_gated` | killed (re-test on full-coverage data) | sanity | First run on partial-coverage data (afternoon-only): IS Sharpe +0.007, walk-forward gate FAIL. Re-run on full-coverage 24h data after MES re-ingest 2026-04-30: per_trade_sharpe -0.380, profit_factor 0.40 — fails sanity gate (raw IS Sharpe > 0). The "barely positive" prior result was an artifact of partial data filtering out morning RTH; honest evaluation kills cleanly. 2026-04-30. |
| H1 | Opening-range failed-breakout fade — price extension beyond the first-30-min RTH range that returns inside the range within K bars indicates the breakout lacked institutional commitment; fade back to mid-range. Event-anchored (fires once per session at most), distinct from the always-on momentum failure mode. | `range_break_fade` (new) / `mes_orb_failure_fade` | killed | sanity | per_trade_sharpe -0.238 over 2718 trades 2019-05 → 2026-04, profit_factor 0.49. The 1-tick re-entry buffer + 5-bar lookback + 12-tick fixed stop fires too easily on minor wicks; cost overhead (~2.2 ticks per round trip = 18% of R) eats the asymmetric R/R distribution. No parameter tuning permitted by sprint rules. 2026-04-30. |
| H3 | Opening-range continuation — confirmed range break with above-average volume, expecting follow-through. Range-anchored (event-localized), inverse mechanism of H1; if H1 fails because fades don't work, continuation is the right side. | `range_break_continuation` (new) / `mes_orb_continuation` | killed | sanity | per_trade_sharpe -0.386 over 3000 trades, profit_factor 0.37. Even worse than the fade variant — both directions of the range-break trade lose money under a fixed-tick-stop framework on MES. Same cost-overhead pattern as H1/H2. 2026-04-30. |

**Sprint outcome (2026-04-30):** All three hypotheses killed at sanity (H1, H3) or sanity-on-re-test (H2). Hypothesis budget exhausted (3/3 hypotheses, 4/12 variants used). Common failure mode across all three: ~20-23% win rate with avg_loss near full stop, indicating the fixed-tick stop + cost-overhead structure (1.2 ticks commission + 1 tick slippage per round trip = 2.2 ticks ≈ 18% of a 12-tick R) consistently eats the asymmetric win/loss distribution. The 12-tick fixed stop is too tight relative to MES intraday true-range (`mes_atr_14m`), so most exits hit the stop before the strategy's mean-reversion / continuation thesis has time to play out.

### Round 3 (2026-05-01) — ATR-stop hypothesis batch

Three new hypotheses, each with 3 pre-registered variants (9 specs total, within the 12-variant budget). The structural change vs. round 2 was a new `atr_multiple` stop class replacing `fixed_ticks`, with stop = entry ± multiplier × `mes_atr_14m`. Hypothesis selection per the rule "different mechanism, not tweaked parameters": none of the round-2 mechanisms were carried over.

| ID | Mechanism (one line) | Strategy class / spec id | Variant axis | Status | Stage of death | Kill reason |
|---|---|---|---|---|---|---|
| N1 | RTH-open gap fill — fade gaps from prior XNYS-session close back toward that close. Inter-session reference, distinct from any intra-session anchor in rounds 1/2. | `gap_fill_fade` (new) / `mes_gap_fill_{a,b,c}` | gap_threshold_pct ∈ {0.3, 0.5, 1.0}% | killed | sanity | All three variants negative Sharpe (best -0.17, worst -0.33). 1080-1453 trades on 0.3-0.5% thresholds; 1.0% threshold drops to 481 trades but Sharpe drops further. Mechanism is closest to working of the round-3 batch but still no edge after costs. |
| N2 | Volatility-compression breakout — when current bar TR < f × ATR_14m, the next bar's break of the compressed bar continues. Local/dynamic range, distinct from session-anchored OR. | `compression_breakout` (new) / `mes_compression_breakout_{a,b,c}` | compression_ratio ∈ {0.3, 0.4, 0.5} | killed | sanity / error | Variant `a` (0.3) errored mid-run on 2020-03-16: at peak COVID volatility, 2×ATR exceeded the 200-tick max_distance_ticks cap. Variants `b` (0.4) and `c` (0.5) ran to completion but fired 3600+ times each over 7 years (≥1.4 trades/day) and posted -0.66 and -0.69 Sharpe. The trigger captures noise more than information. |
| N3 | Volume-spike fade — fade an extreme single-bar volume z-score (≥ threshold) when the next bar fails to extend the spike's direction. Flow-event reference, no price-level anchor. | `volume_spike_fade` (new) / `mes_volume_spike_fade_{a,b,c}` | zscore_threshold ∈ {2.0, 2.5, 3.0} | killed | sanity | Even at z≥3.0 (most selective variant) the strategy fires 3261 times with -0.62 Sharpe. Fading isolated volume spikes does not have edge on MES; the spike-then-no-followthrough condition is too easy to satisfy on a 1m bar series. |

**Round 3 outcome:** 9/9 variants killed at sanity. Hypothesis budget for round 3 fully consumed. The structural switch to ATR-multiple stops did not produce edge — wider stops shifted avg_loss from ~-1.3R (round 2 fixed-tick) to ~-2.0R (round 3 ATR), but win rates stayed in the 17-26% band, so larger losses dominated. **The conclusion is not "ATR stops don't work"; it is "stop sizing isn't the binding constraint when triggers fire 1000-3500 times over 7 years."**

### Cross-sprint synthesis (rounds 1-3, MES, 12 variants total)

What we've now ruled out, with evidence:
- Fixed-tick stops on triggers that fire 100s-1000s of times: 18% cost-overhead eats edge.
- ATR-multiple stops on the same noisy triggers: bigger losses, same edge problem.
- All five tested mechanisms — VWAP fade gated, OR fade, OR continuation, gap fill, compression breakout, volume-spike fade — fail sanity on MES 1m bars when the entry trigger is a single-feature threshold.

What this implies for the next sprint:
1. **Selectivity is the binding constraint, not stop sizing.** A trigger that fires <100 times over 7 years (≤14/year) — by chaining 2-3 conditions — is the structural change worth testing. Until selectivity rises, no risk-frame change will produce edge.
2. **Multi-condition triggers as the new structural axis.** Pre-register hypotheses where entry requires ≥2 independent confirmations (e.g. gap AND volume, compression AND time-of-day, level break AND prior-trend regime).
3. **Gap fill is the closest-to-edge mechanism to date** (round 3 N1 best at -0.17 Sharpe). A revisit with stricter selectivity (e.g. gap-fill only on Mondays after weekend, or only when overnight VIX rose) could test whether the mechanism needs co-conditioning rather than abandonment. This would be a NEW hypothesis (different mechanism = different conditioning), not a tuning of N1.
4. **Coverage-by-hour audit still needed.** Round 2's data discovery (mes_5s_ohlcv only had 14:00-20:00 ET) was almost a sprint-killer. A coverage-by-hour audit at source admission would catch this class of issue.

**Sprint guardrails that worked:**
- Pre-registration of all variants before any backtest run (round 3 batch authored before the first run).
- No parameter tuning after seeing results (none attempted across all 12 variants).
- Honest kill recording — including the ATR-cap error in N2-a.

**Implications for next sprint:**
1. **Stop sizing is load-bearing.** The same hypotheses with ATR-multiple stops (e.g. 1.5–2.5× ATR_14m) might survive — but per anti-overfitting rules, that is a NEW hypothesis batch, not a tweak of these.
2. **Cost-overhead floor.** With current commission + slippage, any strategy with R < 20 ticks pays > 11% of R per round trip. Hypotheses keying on tighter stops are penalized; the harness's CostModel is a hard lower bound.
3. **Mid-RTH triggers fire too often.** All three triggered 1500–3000 times over 7 years. Selectivity gates (regime, session-position, vol band) need to be tighter, OR the trigger itself needs to be more event-specific (e.g. cumulative-volume thresholds, multi-bar confirmation).
4. **Data fix was load-bearing.** H2's prior "barely positive" result was an artifact of partial-coverage data hiding the morning-RTH regime where VWAP fade fails. The 2026-04-30 MES re-ingest (databento OHLCV-1m, 24h coverage) is now the source of record for all derived MES features.

Sprint guardrails ([signal-hunt plan](../.claude/plans/pull-latest-main-review-rippling-unicorn.md)): per-hypothesis variant cap 5; per-sprint cap 3 hypotheses / 12 variants; no parameter tuning after gate failure (the killed-then-tuned anti-pattern from `07_auto_generation.md:194-205`).

---

## Build sequencing (MVP)

Phase 1 (minimum viable):
- Hypothesis DB + schema
- Human submission interface (CLI)
- Literature scanner (one source: SSRN)
- LLM enrichment pipeline with five-test scorer
- Simple triage UI (web or CLI)
- Integration with development pipeline (tickets, pre-registration templates)

Phase 2:
- Market-structure monitor
- Post-session miner
- Duplicate detection via embeddings
- Data feasibility check against feature registry
- Hit-rate and kill-reason dashboards

Phase 3:
- Anomaly detector
- Event calendar scanner
- Additional literature sources
- Automated source-effort tuning based on feedback loops

The anomaly detector is deliberately last. It's the most complex component, the least trustworthy source, and builds on all the earlier infrastructure (duplicate detection, feasibility check, kill-reason tracking). Building it first is a common mistake.

---

## What the system does not do

- **It does not generate strategies autonomously.** Promotion to development requires a human decision, regardless of score.
- **It does not touch live trading.** No hypothesis, at any status, affects live behavior.
- **It does not grade its own outputs.** Quality signal comes from downstream outcomes, not from the system's own scoring.
- **It does not kill hypotheses silently.** Every kill has a recorded reason, reviewable later.
- **It does not create feature requirements or data sources.** If a hypothesis needs data we don't have, that's a separate demand signal to the data-onboarding pipeline. The hypothesis stays dormant until the data exists.

---

## Open design decisions

1. **Embedding model for duplicate detection.** Needs to work well on technical trading concepts. Test several before committing.
2. **Triage quorum.** Single reviewer or multi-reviewer? Leaning single for efficiency, with flagged items escalated to two reviewers.
3. **Dormancy lifecycle.** Do dormant hypotheses auto-expire after N months, or stay dormant forever? Propose: auto-move to `killed` with reason `dormant_expired` after 12 months if unblocker hasn't appeared.
4. **LLM cost budget.** Enrichment consumes LLM tokens. Need a cost cap and batching strategy.
5. **Public hypothesis sharing.** If the system runs at individual-trader scale forever, fine. If scaled to a team, need permissions and attribution on the DB.
