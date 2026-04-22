# Hypothesis System Spec

**Status:** Draft for review
**Purpose:** Define the pipeline that produces, stores, triages, and promotes trading hypotheses. The hypothesis system is upstream of strategy development. It does not generate strategies — it generates the candidate ideas that the strategy pipeline then develops, or kills.

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

Automated: does the data required to test this hypothesis exist in our registered sources / feature catalog at acceptable fidelity?

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
