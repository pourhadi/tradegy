# Feature Pipeline Spec v1

**Status:** Draft for review
**Purpose:** Defines how raw data becomes registered, historically-faithful features
and model-backed features that strategies can safely consume. This pipeline is
upstream of hypothesis generation, strategy development, backtesting, and live
trading — every downstream stage depends on its output.

---

## Design principles

1. **Point-in-time correctness is non-negotiable.** Every feature must be
   reconstructible as it would have appeared at any historical timestamp, with no
   contamination from future information. A feature that cannot demonstrate this
   does not get registered. Full stop.

2. **One bar for admission; no quality ladder.** Every data source either meets
   our requirements or we don't use it. The universal bar: point-in-time queryable,
   deterministic given inputs, declared availability latency, explicit revision
   policy, sufficient coverage for intended use. There are no "lower-fidelity"
   sources admitted with caveats — if a source can't meet the bar, it stays out.
   What we *do* track per source are orthogonal data-model facts (revisability,
   derivation method) that drive storage and versioning, not quality tiers.

3. **Deterministic computation.** Features are pure functions of their declared
   inputs. Same inputs produce identical outputs. No hidden state, no stochastic
   computation without seeded randomness.

4. **Versioned everything.** Data sources, features, models — all versioned. A
   backtest records the exact versions used. Version changes are auditable events,
   not silent updates.

5. **Separation of data, features, and models.** Three distinct registries with
   distinct lifecycles. Data sources feed features. Features may be directly
   computed or model-backed. Models are feature-producers, not decision-makers.

6. **Backfill is not optional.** Every registered feature must have a complete
   historical series available at registration. "We'll backfill later" is how
   point-in-time correctness gets lost. Register when ready, not before.

7. **Live and historical must be the same code path.** The code that produces a
   feature in live must be the same code that produces it for backtest. No
   parallel implementations that silently diverge.

---

## Live/historical parity contract

Principle 7 above is enforced at the source-registration boundary, not just at
the transform layer. **Every registered DataSource pairs two adapters:**

- A **historical** adapter (declared via `ingest:` in the registry YAML) that
  parses bulk files (e.g., Sierra Chart CSV, vendor dumps) and writes rows to
  `data/raw/source=<id>/date=YYYY-MM-DD/data.parquet` in the source's
  canonical schema.
- A **live** adapter (declared via `live:` in the registry YAML, named against
  a registered `LiveAdapter` implementation) that subscribes to the upstream
  real-time feed (e.g., IBKR `reqRealTimeBars` or `reqTickByTickData`
  aggregation) and yields rows in the **same canonical schema**.

The schema (column names, dtypes, units, UTC timezone) is declared by the
DataSource registry entry and is the single source of truth for both
adapters. Downstream readers — feature transforms, the registry API,
backtest harness, live execution — see one shape regardless of which adapter
populated the rows.

**Why this matters:** if the live and historical adapters can drift from
each other in column naming, dtype, or unit conventions, then a feature
computed identically on both will silently produce different values in
backtest vs production — defeating principle 7 at the data layer rather
than the transform layer. The parity contract makes that drift impossible
without breaking a test.

**Enforcement:**
- The `DataSource` schema requires both `ingest` and `live` blocks for any
  source intended for live use. A source that legitimately cannot have a
  live counterpart (test fixtures, archived datasets) declares
  `licensing.live_use: false` with explicit rationale and is excluded from
  live execution at the registry-query layer.
- Parity tests (`tests/integration/test_live_historical_parity.py`)
  capture a small live sample for each parity source and assert column
  parity, dtype parity, and value parity (within tolerance) against the
  historical store. These tests gate the merge that activates a live
  adapter body.

The `LiveAdapter` protocol lives in `src/tradegy/live/base.py` and follows
the same registration discipline as feature transforms (`register_live_adapter`
decorator + lookup table). New venues or alternative-data feeds add a new
adapter as a code change with tests, not a YAML change.

---

## Pipeline stages

Seven stages. Each has defined inputs, outputs, gates, and failure modes.

### Stage 1: Data ingestion

**Inputs:** Raw data in native form — CSV files, API feeds, archive dumps, vendor
downloads.

**Activities:**
- Schema detection and normalization to canonical internal format
- Timestamp normalization to UTC with original timezone preserved as metadata
- Deduplication
- Storage in append-only raw data lake with immutable partitions
- Capture of receipt metadata (when received, from what source, with what transformations)

**Outputs:** Canonically-stored raw data, partitioned by source and time, with
full provenance metadata.

**Gate:** Schema validation passes; no unexplained gaps larger than source's
declared tolerance; receipt metadata complete.

**Failure modes to watch:**
- Silent format drift from source (column order, timestamp format changes)
- Partial files treated as complete
- Timezone ambiguity in DST transitions
- Mixed-resolution data without explicit flagging

### Stage 2: Data audit

**Inputs:** Newly ingested raw data.

**Activities:**
- **Gap detection** — compare observed timestamps to expected cadence; catalog gaps
- **Revision detection** — if updating prior data, identify changed historical values; flag sources that revise
- **Latency characterization** — measure (receipt_time − observation_time); build distribution
- **Cross-source reconciliation** — if multiple sources cover same signal, check consistency
- **Distribution sanity** — values in expected ranges; obvious outliers
- **Calendar compliance** — respects market hours, holidays, expected cadences

**Outputs:** Audit report per ingestion batch, with findings categorized by
severity. Persistent audit record attached to source history.

**Gate:** No CRITICAL findings unreviewed. HIGH findings require documented human
acceptance before source can be used for live trading.

**Failure modes:**
- Revisions going undetected (silent contamination of historical features)
- Latency assumed constant when actually variable
- Missing data forward-filled without audit trail

### Stage 3: Source admission

**Inputs:** Data source + its audit history.

**Activities:** Evaluate the source against the universal admission bar and
record the data-model properties that downstream storage and versioning depend
on.

**Universal admission bar (all required; no exceptions, no overrides):**

- **Point-in-time queryable.** Can reconstruct "what was known at time T" for
  every historical timestamp, bounded by the source's declared revision policy.
- **Deterministic.** Same raw inputs produce identical values on re-fetch or
  replay.
- **Declared availability latency.** A characterized distribution of
  (decision_time − observation_time), not an assumption.
- **Explicit revision policy.** One of `never_revised`, `revised_with_vintages`
  (every prior vintage retained and keyed by transaction time), or `not_admitted`.
  A source that revises without retained vintages cannot meet the bar and is
  rejected; we do not admit sources that would contaminate historical replay.
- **Sufficient coverage.** Continuous series across the window any intended
  consumer needs, with gaps catalogued and bounded.
- **Registered live adapter producing the canonical schema.** A source
  intended for live use must declare a `live:` adapter binding (see the
  Live/historical parity contract section above). A source that
  legitimately cannot have a live counterpart (test fixture, archived
  dataset) must declare `licensing.live_use: false` with explicit
  rationale; it is excluded from live execution at the registry-query
  layer.

A source that fails any check is not admitted. There is no "admit with
caveats" path.

**Data-model properties recorded at admission (not quality ratings; used for
storage and versioning):**

- `revisable: bool` — `false` for append-only series (tick data, receipt-stamped
  captures, finalized publications); `true` for series that publish vintages
  (fundamentals, analyst estimates, some government statistics). Drives storage
  model: append-only time series vs bitemporal (valid_time × transaction_time).
- `derivation: raw | transform | model` — declared on features, not sources, but
  the source admission record notes whether the source itself is raw or is the
  output of an upstream model/vendor pipeline (affects retrain / re-version
  triggers).

**Outputs:** Data source registry entry with admission evidence, revisability
flag, availability-latency characterization, coverage record, and the bound
live-adapter id satisfying the parity contract.

**Gate:** Human review and sign-off on admission. Not automated. An admission
error ships corrupted inputs into every downstream backtest and live decision.

### Stage 4: Feature computation

**Inputs:** Admitted data sources.

**Activities:**
- Execute feature definition (deterministic transform) over data
- Apply availability-latency offset (feature X requires Y seconds post-bar-close
  before it's "available for decisions")
- Produce time-stamped feature series
- Record computation metadata (feature version, code hash, input data versions,
  compute timestamp)

**Feature definition contract:**

```yaml
feature:
  id: "vix_term_structure_slope"
  version: "1.0.0"
  description: "VIX / VIX3M ratio, indicating term structure steepness"
  inputs:
    - source: "cboe_vix"
      min_history_required: "30_days"
    - source: "cboe_vix3m"
      min_history_required: "30_days"
  computation:
    type: "registered_transform"
    transform_id: "ratio"
    parameters:
      numerator: "cboe_vix.close"
      denominator: "cboe_vix3m.close"
  cadence: "daily"
  availability_latency_seconds: 60
  derivation: "transform"              # raw | transform | model
  revisable: false                     # inherits from inputs; true if any input is revisable
  expected_range: [0.5, 2.0]
  outlier_policy: "flag_and_pass"
```

**Revisability and derivation propagation:**
- A feature's `revisable` is `true` if any input source is revisable; otherwise
  `false`. Revisable features are stored bitemporally; non-revisable features
  are append-only.
- A feature's `derivation` is `model` if any input is a model-produced feature;
  otherwise `transform` for composed features or `raw` for direct
  source-passthroughs. Model-derived features trigger retrain/re-version rules
  (see Stage 5).

**Transform registry:** Like strategy class registry, transforms are registered
implementations. Common transforms (rolling_mean, rolling_std, ratio, zscore,
rank, percentile, ewma, etc.) implemented once, unit-tested, referenced by ID.
Adding a new transform type is a code change with tests, not a YAML change.

**Feature inventory growth: vital signs first, hypothesis-driven thereafter.**

*First concrete pull-driven addition (Phase 4): `mes_vwap` was registered
because the `vwap_reversion` strategy class needed it. The feature went
through identical admission gates as the vital-signs set — transform
implementation (`session_vwap`, with unit tests covering per-session
reset and the weighted-mean math), feature YAML, materialize,
no-lookahead audit, reproducibility check. End-to-end backtest of the
strategy then ran successfully against the new feature, demonstrating
the hybrid push/pull model in operation.*



The registry is *not* meant to be a speculatively-pre-populated catalog of
every feature anyone might ever want. We bootstrap it with a thin **vital-
signs** set (≤10 features) covering the axes nearly every futures strategy
keys on: returns at a couple of horizons, volatility (return-based and
range-based), liquidity / activity normalization, and time-of-day. Beyond
that, **features are added on demand from hypothesis specs**: a hypothesis
declares the features its mechanism keys on (`06_hypothesis_system.md`),
and any feature not already in the registry triggers a registration
request that goes through the normal admission gates (transform
implementation, no-lookahead audit, reproducibility check, backfill
coverage).

This is a hybrid push/pull model:

| | Vital signs (push) | Hypothesis-driven (pull) |
|---|---|---|
| **Trigger** | Pre-registered by the platform team | Requested by a hypothesis spec |
| **Goal** | Bootstrap the strategy/backtest layer with realistic compositional surface | Avoid speculative inventory; let real strategy demand shape the registry |
| **Gating** | Same admission gates | Same admission gates |
| **Cap** | ≤10 features; expand only via the pull path | Unbounded but rate-limited by mechanism articulation |

The push/pull distinction is about *who initiates registration*, not about
gating discipline — both paths run the same audits and produce the same
backfilled, point-in-time-correct, parity-contract-compliant registry
entries. Pure pull would lose the validation gates (ad-hoc per-hypothesis
features bypass the no-lookahead audit and the live/historical parity
contract); pure push would build inventory no strategy ever uses. The
hybrid avoids both failure modes.

**Live adapter registry:** Same discipline applies to the live half of the
parity contract. A `LiveAdapter` (`src/tradegy/live/base.py`) is an async
class with a small lifecycle (`connect`, `disconnect`, `subscribe(spec) ->
AsyncIterator[BarRow]`, `health`). Each implementation registers under a
string name via the `register_live_adapter` decorator; YAML registry
entries name adapters via that string in the `live.adapter` field. The
`BarRow` dataclass declares the canonical row shape (`ts_utc`, `open`,
`high`, `low`, `close`, `volume`, plus optional `num_trades`,
`bid_volume`, `ask_volume`); historical and live both produce rows
matching this shape. New venues or alternative-data feeds add an adapter
as a code change with tests, not a YAML change.

**Outputs:** Historical feature series stored with version metadata.

**Gate:** Transform resolves to registered implementation; inputs available at
required history; no-lookahead check passes (feature at T depends only on inputs
available before T + availability_latency).

### Stage 5: Model training (for model-backed features)

**Inputs:** Registered features (feature models consume other features as inputs).

**Activities:**
- Train model per declared spec
- Validate on held-out data with walk-forward methodology
- Calibrate if producing probabilistic outputs
- Freeze model artifact with exact training data hash, hyperparameters, code version
- Generate historical predictions via walk-forward replay (each historical
  prediction made by a model trained only on data available before that prediction's
  timestamp)

**Model definition contract:**

```yaml
model:
  id: "regime_classifier_hmm"
  version: "2.1.0"
  description: "HMM classifying sessions into 4 regime states"
  model_class: "hmm_gaussian"
  inputs:
    - feature: "returns_5min"
    - feature: "realized_vol_30min"
    - feature: "volume_zscore_20bar"
  output_feature_id: "regime_probabilities"
  training:
    window: "rolling_3_years"
    retrain_cadence: "quarterly"
    validation_method: "walk_forward_12_folds"
  hyperparameters:
    n_states: 4
    covariance_type: "full"
    random_seed: 42
  calibration:
    method: "isotonic"
  replay_constraints:
    historical_prediction_method: "walk_forward_replay"
    no_retroactive_training: true
```

**Model class registry:** Analogous to strategy class registry. `hmm_gaussian`,
`xgboost_classifier`, `logistic_regression`, `random_forest`, `lstm_sequence`,
etc. Each class has an enforced interface (fit, predict, serialize, deserialize,
version metadata).

**Walk-forward replay for historical predictions:**

The critical anti-leakage mechanism. To produce a historical prediction for T:
1. Train the model on data from [T − training_window, T − min_gap]
2. Apply to produce prediction for T
3. Never use data from T onward in that prediction

Result: historical prediction series that's actually replayable, not
retroactively-modeled. Expensive (train many model versions to produce history)
but the only honest way. Shortcuts here are how leakage happens.

**Outputs:** Frozen model artifact + historical prediction series + training
metadata. Prediction series becomes a registered feature.

**Gate:** Walk-forward validation passes; leakage audit passes; calibration
verified; model artifact serializable and deterministic on reload.

**Failure modes:**
- "Retroactive training" — training on all history and applying to history (massive leakage)
- Hyperparameter selection using out-of-sample data
- Training data boundaries that leak via correlated features
- Non-deterministic models without seeded randomness producing different predictions on replay

### Stage 6: Feature validation

**Inputs:** Computed historical feature series (or model-generated series).

**Activities:**

- **No-lookahead audit.** For a random sample of historical timestamps, verify
  that the feature value at T depends only on inputs available before T +
  availability_latency. Automated tooling reconstructs the feature using only
  pre-T data and compares to the published series.

- **Distribution checks.** Values within declared expected range (or outliers
  flagged per policy). Distribution stability over time. No infinities/NaNs/
  impossible magnitudes.

- **Gap consistency.** Feature gaps match data source gaps.

- **Cadence compliance.** Feature produced at declared cadence.

- **Reproducibility check.** Recompute a random sample from raw data; must match
  stored values exactly.

**Outputs:** Validation report, signed by harness.

**Gate:** All checks pass. Any failure blocks registration.

### Stage 7: Registration

**Inputs:** Validated feature series with complete metadata.

**Activities:**
- Assign feature ID and version
- Record full provenance (data sources, versions, computation spec, validation results)
- Store in feature registry with queryable API
- Make historical series available via standard feature-retrieval interface
- Announce registration event (downstream systems subscribe for new features)

**Outputs:** Registered feature, queryable via registry API.

**Gate:** Human review of registration packet for features destined for live use.
Automated registration acceptable only for features remaining in the
`in_development` or `research` lifecycle state.

---

## Data source registry schema

A registered source pairs an `ingest:` block (the historical adapter — how
to parse the bulk file) with a `live:` block (the live adapter binding) and
declares the canonical row schema both adapters must produce. This example
matches the shape of `registries/data_sources/mes_5s_ohlcv.yaml`.

```yaml
data_source:
  id: "mes_5s_ohlcv"
  version: "v1"
  description: "MES continuous front-month 5-second OHLCV bars with signed-flow proxies"
  type: "market_data"  # market_data | economic | news | alternative | derived
  provider: "sierra_chart_then_ibkr"
  revisable: false
  revision_policy: "never_revised"
  admission_rationale: |
    OHLCV bars from Sierra Chart are append-only; venue-published flow
    metadata is not retroactively revised. Live and historical are produced
    under the same canonical schema by paired adapters; the parity contract
    is enforced at registration time.
  coverage:
    start_date: "2019-05-06"
    end_date: "2025-08-13"
    gaps: []
  cadence: "5s"
  fields:
    - {name: ts_utc, type: timestamp}
    - {name: open, type: float}
    - {name: high, type: float}
    - {name: low, type: float}
    - {name: close, type: float}
    - {name: volume, type: int}
    - {name: num_trades, type: int}
    - {name: bid_volume, type: int}
    - {name: ask_volume, type: int}
  ingest:
    format: sierra_chart_csv         # generic_csv | sierra_chart_csv
    timestamp_columns: [Date, Time]  # multi-column for Sierra
    column_remap:
      Last: close
      NumberOfTrades: num_trades
      BidVolume: bid_volume
      AskVolume: ask_volume
  live:
    adapter: ibkr_realtime_bars_5s   # registered LiveAdapter id
    params:
      symbol: MES
      exchange: CME
      currency: USD
      useRTH: false
  session_calendar: CMES             # exchange_calendars name; gates excessive_gap audit
  availability_latency:
    median_seconds: 0.0
    p99_seconds: 0.5
    notes: "Historical CSV has zero replay latency; live reqRealTimeBars sub-second."
  licensing:
    live_use: true
    backtest_use: true
    redistribution: false
  known_issues:
    - "Continuous-contract roll method TBD; verify by inspecting price gaps near quarterly expiries."
  audit_history: []
```

Notes:

- `ingest.format` is a discriminator the CLI dispatches on. `generic_csv`
  retains the legacy single-`timestamp_column` path; `sierra_chart_csv`
  uses the multi-column timestamp parsing and column remap shown above.
- `live.adapter` must name a `LiveAdapter` registered via
  `register_live_adapter(...)`. Adapter parameters that vary per source
  (contract specifier, useRTH, exchange) live in `live.params`; connection
  parameters (host/port/clientId) live in env vars (e.g., `IBKR_HOST`)
  rather than YAML so registry entries stay portable across machines.
- `session_calendar` opts the source into session-aware audit gating —
  `excessive_gap` will not fire on overnight maintenance halts, weekends,
  or holidays for the named calendar (`CMES` for CME futures).

## Feature registry schema

```yaml
feature:
  id: "realized_vol_30min"
  version: "1.2.0"
  description: "30-minute realized volatility, annualized, from 1-min log returns"
  type: "derived"  # raw | derived | model_backed
  inputs:
    - source_id: "ib_es_ticks"
      resampled_to: "1min_ohlc"
  computation:
    transform_id: "annualized_realized_vol"
    parameters:
      lookback_minutes: 30
      return_type: "log"
      annualization_factor: "intraday_252_78"
  cadence: "1min"
  availability_latency_seconds: 5
  derivation: "transform"
  revisable: false
  expected_range: [0.05, 2.0]
  outlier_policy: "flag_and_pass"
  historical_coverage:
    start: "2020-01-06"
    end: "current"
  dependent_models: []
  dependent_strategies:
    - "es_orb_fade_v1"
    - "es_vwap_reversion_v2"
  validation_record:
    no_lookahead_audit: "passed_2024-11-15"
    reproducibility: "passed_2024-11-15"
    distribution_stability: "passed_2024-11-15"
  version_history:
    - version: "1.0.0"
      date: "2024-03-01"
      change: "initial"
    - version: "1.1.0"
      date: "2024-07-15"
      change: "changed annualization to intraday-specific factor"
    - version: "1.2.0"
      date: "2024-11-15"
      change: "excluded first 5 min after open from realized vol calc"
```

## Model registry schema

```yaml
model:
  id: "regime_classifier_hmm"
  version: "2.1.0"
  # ... (core fields as in Stage 5)
  produces_feature: "regime_probabilities_v2"
  training_history:
    - training_date: "2024-10-01"
      training_window_start: "2021-10-01"
      training_window_end: "2024-09-30"
      data_hash: "sha256:..."
      artifact_hash: "sha256:..."
      validation_metrics:
        out_of_sample_log_loss: 0.42
        regime_transition_accuracy: 0.78
  prediction_history_method: "walk_forward_replay"
  retrain_schedule:
    cadence: "quarterly"
    next_retrain_date: "2025-01-01"
  monitoring:
    drift_detection_enabled: true
    performance_thresholds:
      log_loss_max: 0.6
      action_on_breach: "flag_for_review"
```

---

## Live feature computation

Historical computation produces the backtest series. Live computation produces
real-time features for the trading system. Critical requirement: **same code.**

- Feature computation code lives in a single implementation per feature
- Historical mode: consume from data lake, produce full historical series
- Live mode: consume from real-time feed, produce streaming feature values
- Both modes enforce the same availability_latency; in live, the value for bar T
  is not published until T + availability_latency

**No parallel implementations.** If there's a "backtest version" and a "live
version" written separately, they will diverge. Single codebase, multiple
execution modes.

**Live-vs-historical drift monitoring.** For every feature in live, continuously
compare: are live values in the same distribution as historical values for
comparable conditions? Drift alerts flag features whose live behavior diverges
from historical — usually indicating a data source issue, computation bug, or
real regime change that invalidates the feature.

---

## The "accept a CSV" intake workflow

Concrete user-facing workflow for onboarding a new dataset:

1. **Drop CSV into intake directory** (`/data/intake/pending/`)
2. **Automated detection** runs schema inference; produces candidate normalization
   mapping; flags ambiguities for human review
3. **Human confirms schema** via CLI or web prompt; corrections applied
4. **Automated ingestion** moves data into raw data lake with canonical schema
5. **Automated audit** runs full Stage 2 audit suite; produces audit report
6. **Human reviews audit** and proposes admission with rationale against the
   universal bar (point-in-time, determinism, availability latency, revision
   policy, coverage) plus revisability and derivation annotations
7. **Registration** of data source pending admission review; second human signs
   off before source becomes available to features
8. **Standard feature battery** (user opts in per source) computes a default set
   of derived features (returns, vol, range, zscore, percentile rank, etc.)
   automatically
9. **Custom features** proposed via feature spec YAML; go through Stages 4–7
   individually
10. **Source becomes available** to hypothesis generator and strategy pipeline

Time from drop to usable: hours to days, depending on audit findings. Not minutes.
The deliberate friction is the point.

---

## Monitoring and lifecycle

**Ongoing monitoring per registered feature:**
- Distribution drift vs historical (alert on significant divergence)
- Computation latency (is live computation still meeting declared latency?)
- Upstream data source health
- Dependent strategy health

**Feature lifecycle states:**
- `in_development` — being built, not available to strategies
- `research` — available to hypothesis generation and research backtests, not live
- `live` — available for live trading strategies
- `deprecated` — still computed for backward compatibility, but not for new strategies
- `retired` — no longer computed; historical series preserved, not updated

**Feature retirement:** Retirement requires no live strategy depending on it.
Retiring a feature in use requires migration of dependent strategies first.

**Version updates:** A feature version update is a MAJOR event:
1. New version computed and registered alongside old
2. Regression testing runs every dependent strategy against new version, compares performance
3. Material divergence triggers review before dependent strategies migrate
4. Migration is explicit per strategy (version bump in strategy spec), not automatic
5. Old version remains available until no strategy references it

---

## Integration points with other pipelines

**Upstream of:**
- Hypothesis generation (queries feature/data catalog for what's available)
- Auto-generator (references feature registry when drafting specs)
- Backtest harness (validates spec feature requirements; produces feature-version-stamped results)
- Live trading system (consumes real-time feature stream)
- Library monitoring (tracks feature-level P&L attribution across strategies)

**Queries exposed via registry API:**
- "Give me all features with cadence ≤ 5min, availability_latency ≤ 10s, and coverage spanning [T₀, T₁]"
- "For feature X, what strategies depend on it, at what versions"
- "For strategy Y, what features does it depend on, with what revisability and derivation"
- "What's the current runtime health (green/degraded/stale/failed) for feature Z"
- "Audit trail: what was feature X's value at time T, per version V (and per source vintage, if revisable)"

---

## What's deliberately NOT in this pipeline

- **Strategy-level decisions.** Features don't decide anything. They produce numbers.
- **Automatic feature engineering.** No "try 10000 feature combinations and pick best." Features are human-proposed (possibly LLM-drafted), reviewed, and registered deliberately.
- **Free-form model architectures.** Models use registered model classes with declared hyperparameters. Novel architectures require code-level additions with tests.
- **Real-time retraining.** Models retrain on scheduled cadences (quarterly typically), not continuously. Continuous retraining creates untestable moving targets.

---

## MVP and sequencing

**Phase 1 (must exist before any strategy work):**
- Data ingestion for ES tick data (single admitted source, non-revisable)
- Data audit suite
- Feature computation for 15–20 canonical price-derived features (returns at
  multiple horizons, realized vol, range, VWAP, volume features, time-of-day
  features)
- Feature registry with basic query API
- No-lookahead audit tooling

**Phase 2 (before advanced strategies):**
- Additional data sources: VIX/VIX3M, economic calendar, options chains
- Cross-asset features, calendar features, options-derived features
- First model-backed features (HMM regime classifier)

**Phase 3 (enabling richer hypotheses):**
- News/sentiment ingestion — only admitted if the source can meet the universal
  bar (point-in-time queryable with retained vintages for any revisions, or
  non-revisable captures stamped at receipt). Sources that apply retroactive
  model rescoring to historical text are not admitted.
- Alternative data onboarding framework (same bar; no carve-outs)
- Advanced model types (sequence models, tree ensembles)

**Phase 4 (maturation):**
- Feature marketplace/discovery UI
- Automated feature-candidate scoring for hypothesis generator
- Cross-feature correlation analysis and redundancy detection

---

## Open design decisions for v1.0 freeze

1. **Storage format.** Parquet + date partitioning seems obvious but worth
   confirming against query patterns. Alternatively, a time-series DB (QuestDB,
   kdb+) for hot features and Parquet for cold.

2. **Feature-retrieval API shape.** Pull (query at read time) vs push (stream to
   subscribers)? Probably both — backtest wants pull, live wants push.

3. **Feature computation scheduling.** Event-driven (compute when upstream data
   arrives) vs scheduled (compute at fixed cadences)? Leaning event-driven for
   most features with scheduled batch for end-of-day aggregates.

4. **Who owns model drift decisions?** Drift detection is automated; decision to
   retrain off-cycle or retire a model should be human-gated. Need to define the
   workflow.

5. **Feature spec authoring — LLM-assisted?** Similar to strategy specs, LLMs
   are probably useful for drafting feature specs from natural-language
   descriptions. Same review pattern: LLM drafts, human reviews, human signs off.
