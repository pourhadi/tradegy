# Strategy Spec Schema v1

**Status:** Draft for review
**Purpose:** Defines the structure of every entry in the strategy library. This is the
contract between the humans/LLMs who author strategies, the harness that validates
them, and the selection layer that picks them at runtime.

---

## Design principles

These shape every field decision below. If a proposed field violates one of these, it
doesn't go in.

1. **Mechanical, not discretionary.** Entry, stop, sizing, and exit rules reference
   registered strategy classes with typed parameters. No free-form logic in YAML, no
   embedded Python, no "trader uses judgment here." The backtest harness must be able
   to execute the spec deterministically.

2. **Two audiences, one document.** The spec serves the harness (needs parseable
   mechanics) and the selection LLM (needs prose context). Every section is explicit
   about which audience it serves.

3. **Human-authored config vs. machine-authored evidence.** Humans write *what the
   strategy is*. The harness writes *what the evidence says about it*. These sections
   are separated and the harness refuses to run if a human has tampered with the
   evidence section.

4. **Versioned and immutable per version.** Any material change to a strategy bumps
   the version. Validation evidence is tied to a specific version. You can't quietly
   change parameters and keep the old Sharpe ratio.

5. **Retirement is a first-class state.** Strategies can and should leave the library.
   The spec encodes the conditions under which that happens.

6. **The spec is the primary artifact.** Code implementations of strategy classes are
   referenced by ID. The spec is what humans review, version-control, and approve.

---

## Schema structure

A strategy spec is a YAML document with these top-level sections:

| Section | Audience | Authored by | Mutable after approval? |
|---|---|---|---|
| `metadata` | both | human | version bump only |
| `market_scope` | both | human | version bump only |
| `entry` | harness | human | version bump only |
| `sizing` | harness | human | version bump only |
| `stops` | harness | human | version bump only |
| `exits` | harness | human | version bump only |
| `parameter_envelope` | harness | human | version bump only |
| `context_conditions` | LLM | human (LLM-drafted, human-approved) | version bump only |
| `retirement_criteria` | harness | human | version bump only |
| `operational` | harness | human | see field-level notes |
| `backtest_evidence` | both | **harness** | append-only |
| `validation_record` | both | **harness** | append-only |
| `live_performance` | harness | **harness** | append-only |

---

## Section: metadata

Identity and provenance. Read by everyone.

```yaml
metadata:
  id: "es_opening_range_breakout"          # stable slug, lowercase, snake_case
  version: "1.2.0"                          # semver — bump per change class below
  schema_version: "1.0"                     # which version of THIS schema it conforms to
  name: "ES Opening Range Breakout"         # human display name
  status: "paper_trading"                   # draft | in_validation | paper_trading | live | retired
  created_date: "2026-03-15"
  last_modified_date: "2026-04-18"
  author: "dan"
  reviewers: ["dan", "claude"]              # who signed off on this version
  parent_strategy_id: null                  # if variant, points to parent
  description: |                            # one paragraph, human-readable
    Fades the first N-minute range break on ES when volume fails to confirm.
    Designed for balanced-open sessions without imminent macro catalysts.
```

**Version bumping rules:**

- `PATCH` (1.2.0 → 1.2.1): documentation-only changes, typo fixes in prose
- `MINOR` (1.2.0 → 1.3.0): tightening of parameter ranges within previously tested
  envelope; additions to `known_failure_modes`; new context descriptions
- `MAJOR` (1.2.0 → 2.0.0): any change to mechanical rules, parameter values outside
  previously validated envelope, change to strategy class. Requires full revalidation.

The harness refuses to promote a MAJOR-bumped strategy to live without fresh
validation evidence.

---

## Section: market_scope

What the strategy trades and when.

```yaml
market_scope:
  market: "ES"                              # currently ES only; field reserved for future
  instrument: "MES"                         # MES (micro) | ES (full). Capital-dependent.
  session: "RTH"                            # RTH | globex | both
  time_windows:                             # when entries are allowed (exits can extend)
    - start: "09:30"
      end: "11:00"
      timezone: "America/New_York"
  blackout_dates:                           # categorical exclusions
    - type: "fomc_announcement"
      window_before_minutes: 120
      window_after_minutes: 60
    - type: "cpi_release"
      window_before_minutes: 60
      window_after_minutes: 30
    - type: "non_farm_payrolls"
      window_before_minutes: 60
      window_after_minutes: 30
  day_of_week_filter: ["MON", "TUE", "WED", "THU"]   # exclude Fridays if desired
```

Rationale for the blackout DSL: we want the LLM selection layer to be able to reason
about "is this strategy allowed right now?" as a boolean check, not by parsing prose.

---

## Section: entry

The mechanical entry rule. Harness-executable.

```yaml
entry:
  strategy_class: "range_break_fade"        # MUST be in the registered class catalog
  parameters:
    range_window_minutes: 30
    break_trigger_ticks: 2                  # how far past range extreme counts as break
    volume_confirmation:
      enabled: true
      lookback_bars: 20
      required_multiple: 1.2                # volume on break bar must be >= 1.2x avg
    max_attempts_per_session: 1
  direction: "both"                         # long | short | both
  entry_order_type: "limit"                 # limit | market | stop
  limit_offset_ticks: 1                     # if limit, how aggressive
```

**Registered class invariant:** `strategy_class` must resolve to an implementation in
the strategy class registry. The harness checks this at spec load time. There is no
way to define a novel strategy class in YAML — that requires a code change, a unit
test suite for the class, and a class-registry PR review.

This is deliberate. The friction is the feature.

---

## Section: sizing

```yaml
sizing:
  method: "fixed_fractional_risk"           # see sizing class registry
  parameters:
    risk_per_trade_pct: 0.5                 # % of account equity risked per trade
    max_contracts: 2                        # hard cap regardless of calculation
  scaling: null                             # no position averaging for this strategy
```

Sizing classes are registered similarly to entry classes. Options include:
`fixed_contracts`, `fixed_fractional_risk`, `volatility_scaled`, `kelly_fraction`.

Scaling (position averaging) is a separate structured object when enabled, with its
own class registry and explicit rules for add-triggers and max adds. Strategies that
allow scaling must have scaling behavior validated separately in backtest.

---

## Section: stops

```yaml
stops:
  initial_stop:
    method: "opposite_range_extreme"        # stop class registry
    buffer_ticks: 2
  adjustment_rules:
    - trigger: "profit_equals_initial_risk"
      action: "move_to_breakeven"
      offset_ticks: 0
    - trigger: "bars_since_entry >= 10"
      action: "tighten"
      new_distance_ticks: 8
  hard_max_distance_ticks: 40               # absolute ceiling regardless of method
  time_stop:
    enabled: true
    max_holding_bars: 24                    # 24 5-min bars = 2 hours
    action_at_time_stop: "exit_market"
```

`hard_max_distance_ticks` is a guardrail: no combination of methods and parameters
can produce a stop wider than this. The harness validates this at spec load time.

---

## Section: exits

```yaml
exits:
  profit_targets:
    - level: "1R"                           # R = initial risk distance
      size_pct: 50                          # take half off at 1R
    - level: "2R"
      size_pct: 50                          # remainder at 2R
  invalidation_conditions:                  # thesis-break exits
    - condition: "price_returns_to_range_midpoint"
      action: "exit_market"
    - condition: "opposite_direction_break_confirmed"
      action: "exit_market"
  end_of_session:
    action: "flatten_before_close"
    minutes_before_close: 15
```

Invalidation conditions reference a registered set of checkable conditions, not
free-form rules. If you want a new condition type, it's a code change and a registry
entry.

---

## Section: parameter_envelope

The envelope of parameter values that have been validated. This is different from
the parameters currently in use (which live inside `entry`, `sizing`, etc.).

```yaml
parameter_envelope:
  range_window_minutes: {tested_min: 15, tested_max: 45, step: 5}
  break_trigger_ticks: {tested_min: 1, tested_max: 4, step: 1}
  volume_required_multiple: {tested_min: 1.0, tested_max: 1.5, step: 0.1}
  risk_per_trade_pct: {tested_min: 0.25, tested_max: 1.0, step: 0.25}
  max_holding_bars: {tested_min: 12, tested_max: 36, step: 6}
```

**Invariant:** every parameter in `entry`, `sizing`, `stops`, `exits` with a numeric
value must have its current value fall within its declared envelope. The harness
refuses to load a spec where current parameters lie outside the tested envelope.

This is the mechanism that prevents quiet drift: if you want to run the strategy at
`range_window_minutes: 60`, you must first run the validation suite on that value,
record the evidence, and update the envelope. It's a deliberate speed bump on
changes.

---

## Section: context_conditions

**This is the field the selection LLM reads.** It deserves special care.

```yaml
context_conditions:
  structured:
    preferred_regimes: ["range_bound", "balanced_open"]
    avoided_regimes: ["strong_trend_continuation", "gap_and_go"]
    volatility_range:
      min_vix: 12
      max_vix: 28
    trend_state: ["neutral", "weak"]        # NOT for strong trend days
    required_conditions:
      - "no_major_macro_event_within_60min"
      - "overnight_range_normal"            # not gap > 0.5%
    disqualifying_conditions:
      - "fomc_day"
      - "cpi_morning"
  when_to_use: |                            # prose, crisp, for LLM consumption
    Deploy on balanced-open sessions where overnight range is normal (gap < 0.5%)
    and VIX sits in the 12-28 band. Works best when the first 30-minute range is
    tight relative to prior session (compression signal). Prefer no major scheduled
    economic release in the morning window.
  when_not_to_use: |                        # anti-patterns, equally crisp
    Do not deploy on FOMC days, CPI mornings, or when overnight gap exceeds 0.5%.
    Avoid when VIX is above 28 (range breaks tend to extend in high-vol regimes,
    fading them has poor expectancy) or below 12 (ranges too tight to generate
    meaningful R). Stand down on obvious trend-continuation days (three consecutive
    same-direction sessions with closes near extremes).
  historical_best_conditions: |             # evidence-backed, from backtest
    Best quintile of historical performance: VIX 15-22, overnight gap < 0.3%,
    prior-day range in 40th-70th percentile of 20-day distribution, no macro
    release in RTH window. Sharpe in this subset: 2.1 vs. overall 0.9.
```

**Authoring protocol for `context_conditions`:**

1. LLM drafts the prose fields from the backtest regime-stratification output.
2. Human reviews for accuracy against the evidence.
3. Human signs off; sign-off recorded in metadata.reviewers.

This field is uniquely dangerous because it reads authoritative but drives live
selection. The review step is non-optional.

---

## Section: retirement_criteria

When to pull this strategy out of live selection.

```yaml
retirement_criteria:
  quantitative_triggers:
    - metric: "rolling_20_trade_sharpe"
      threshold: -0.5
      action: "flag_for_review"
    - metric: "rolling_20_trade_sharpe"
      threshold: -1.0
      action: "auto_disable"                # pulled from live, human must re-enable
    - metric: "drawdown_from_peak_pct"
      threshold: 15
      action: "auto_disable"
    - metric: "consecutive_losses"
      threshold: 6
      action: "flag_for_review"
  qualitative_triggers:                     # human-evaluated, documented here
    - "structural_regime_change"            # e.g., switch to continuous trading hours
    - "strategy_class_retired"              # the underlying class was retired
    - "better_variant_promoted"             # a strictly-better version exists
  minimum_trades_before_retirement_eligible: 40
```

`auto_disable` is irreversible by the system itself. A human must actively re-enable
a retired strategy, and re-enabling requires a fresh validation run.

---

## Section: operational

Runtime controls. Some fields here are mutable without a version bump, because they
represent live operational state, not strategy design.

```yaml
operational:
  enabled: true                             # MUTABLE — operator can disable without version bump
  live_since: "2026-04-01"
  risk_envelope:                            # hard bounds the selection LLM cannot violate
    max_concurrent_instances: 1             # can this strategy be running twice at once? no.
    max_daily_loss_pct: 1.5
    max_weekly_loss_pct: 3.0
  incompatible_with:                        # can't be active simultaneously with these
    - "es_trend_continuation"
  tier: "auto_execute"                      # auto_execute | confirm_then_execute | proposal_only
```

`tier` maps to the three-tier safety model:
- `auto_execute`: selection LLM picks it, tactical layer runs it.
- `confirm_then_execute`: selection LLM proposes, human confirms (mandatory during
  paper trading and early live).
- `proposal_only`: logged for review, never traded.

`enabled` and `tier` are the only operational fields that can change without a
version bump. All changes to these are logged separately in an ops audit trail.

---

## Section: backtest_evidence (harness-authored)

Humans do not write this. The harness produces it, signs it, and the spec validator
checks the signature. Any human modification invalidates the spec.

```yaml
backtest_evidence:
  harness_version: "0.4.1"
  data_range:
    start: "2019-01-02"
    end: "2025-12-31"
  in_sample_range:
    start: "2019-01-02"
    end: "2023-12-31"
  out_of_sample_range:
    start: "2024-01-01"
    end: "2025-12-31"
  cost_model:
    commission_per_contract_round_trip: 1.50
    slippage_ticks_per_side: 0.5
    margin_cost_annual_rate: 0.06
  aggregate_stats:                          # all post-cost
    total_trades: 247
    expectancy_R: 0.34
    sharpe: 0.91
    sortino: 1.28
    max_drawdown_pct: 8.7
    win_rate: 0.52
    avg_win_R: 1.42
    avg_loss_R: -0.83
    avg_holding_time_minutes: 38
    worst_month_pct: -4.2
    best_month_pct: 5.1
  regime_stratified:
    range_bound_sessions:
      sharpe: 1.8
      trades: 112
    trending_sessions:
      sharpe: -0.3
      trades: 84
    high_vol_sessions:
      sharpe: 0.4
      trades: 51
  parameter_sensitivity:
    passed: true
    worst_perturbation_sharpe_delta: -0.28  # within ±20% sweep
  baseline_comparisons:
    buy_and_hold_sharpe: 0.6
    random_entry_same_holding_sharpe: -0.1
    simple_regime_rule_sharpe: 0.5
  generated_at: "2026-03-14T18:22:10Z"
  harness_signature: "sha256:..."           # signed hash of stats + inputs
```

---

## Section: validation_record (harness-authored)

```yaml
validation_record:
  cpcv:
    n_folds: 10
    n_paths: 100
    purged: true
    embargo_pct: 2
    median_sharpe: 0.87
    sharpe_iqr: [0.52, 1.15]
    pct_paths_negative: 12
    passed: true                            # < 20% negative paths required
    run_date: "2026-03-14"
  walk_forward:
    n_windows: 12
    avg_out_of_sample_sharpe: 0.81
    worst_window_sharpe: 0.12
    passed: true
  paper_trading:
    start_date: "2026-04-01"
    end_date: null                          # ongoing
    trades: 23
    paper_sharpe: 0.74
    divergence_from_backtest_sharpe: -0.17  # within acceptable band
    passed: null                            # not yet complete
  human_signoffs:
    - reviewer: "dan"
      date: "2026-03-16"
      stage: "validation_complete"
      notes: "CPCV results consistent with in-sample. Proceeding to paper."
```

---

## Section: live_performance (harness-authored, append-only)

Ongoing performance, updated per trade. This is what the retirement-criteria engine
watches.

```yaml
live_performance:
  first_trade: "2026-04-01"
  last_updated: "2026-04-20"
  total_trades: 23
  realized_pnl_R: 7.8
  rolling_20_sharpe: 0.74
  drawdown_from_peak_pct: 3.2
  envelope_status: "within_expected_band"   # within | caution | breach
  envelope_breaches: []
```

---

## Validation invariants (harness checks at load time)

The harness refuses to load a spec that violates any of these:

1. `metadata.id` is unique in the library.
2. `metadata.schema_version` is a supported version.
3. All referenced classes (`entry.strategy_class`, sizing methods, stop methods,
   invalidation conditions) exist in their respective registries.
4. All numeric parameters in `entry`, `sizing`, `stops`, `exits` lie within their
   declared `parameter_envelope`.
5. `stops.hard_max_distance_ticks` is respected by every stop method + parameter
   combination.
6. `operational.risk_envelope.max_daily_loss_pct` is ≤ the portfolio-level cap.
7. `backtest_evidence.harness_signature` validates against the stats.
8. If `status == "live"`: `validation_record.cpcv.passed == true`,
   `validation_record.walk_forward.passed == true`,
   `validation_record.paper_trading.passed == true`, and at least one
   `human_signoffs` entry exists with stage `"promotion_to_live"`.
9. If `operational.tier == "auto_execute"`: status must be `"live"`.
10. `retirement_criteria.quantitative_triggers` includes at least one `auto_disable`
    trigger (dead-man switch requirement).

---

## Schema versioning and migration

- The schema itself is versioned. Specs declare `schema_version` and the loader
  dispatches accordingly.
- When the schema gains a backward-compatible field, we bump the schema MINOR
  version. Existing specs continue to load.
- When the schema changes a field incompatibly, we bump the schema MAJOR version
  and provide a migration script. Specs must be migrated before they'll load under
  the new schema. The old loader remains available for reading historical records.
- The schema has its own changelog, in the same repo as this document.

---

## What's deliberately NOT in the schema

A few things that might seem obvious to include but are intentionally out:

- **Live P&L in currency.** We record R-multiples and percentages. Currency numbers
  depend on account size and shift when we scale capital. R-multiples don't.
- **Broker-specific order routing details.** That's execution-layer config, not
  strategy config. The strategy says "limit order"; the execution layer decides
  which route.
- **Free-form notes field.** Tempting, but becomes a dumping ground. All content
  goes in structured fields with defined purposes.
- **Feature-engineering code.** The features the strategy depends on come from the
  feature pipeline, referenced by feature IDs. A strategy says "uses feature
  `vix_level`"; the feature pipeline defines what that is. Same registry principle
  as strategy classes.

---

## Open design decisions (need resolution before v1.0 freeze)

1. **Feature dependency declaration.** Should strategies declare which features
   (VIX level, regime probabilities, volume profile, etc.) they depend on, so the
   selection layer can verify those features are healthy before selecting? I lean
   yes — add a `feature_dependencies` section to `entry`.

2. **Portfolio-level composition rules.** `operational.incompatible_with` handles
   pairwise conflicts, but we may need a richer composition language (e.g., "never
   run more than 2 breakout strategies simultaneously"). Defer to v1.1?

3. **Partial-fill and rejection handling.** Where does "what happens if the entry
   limit isn't filled within N seconds" live — in the spec, or in the execution
   layer's default behavior? I lean execution layer, but it needs to be consistent
   across specs.

4. **Human-in-the-loop confirmation UI hooks.** For `confirm_then_execute` tier,
   what does the confirmation payload look like? Probably deserves its own mini-
   schema within the spec.

5. **Strategy composition.** Can a strategy reference another strategy for its
   exit logic (e.g., "enter with ORB, exit with vwap_reversion-style logic")? I
   lean no for v1 — too much interaction complexity — but it's a natural extension.

6. **Multi-timeframe specs.** The current schema assumes a single entry timeframe.
   Should we support strategies that combine, e.g., 5-min entry triggers with
   daily-timeframe regime filters? Daily filters are currently expressed via
   `context_conditions`, which is LLM-readable but not mechanically enforced. May
   need a `gating_conditions` mechanical section.

---

## Next artifacts that reference this schema

- **Strategy class registry spec** — catalog of implementable classes and their
  parameter contracts.
- **Feature registry spec** — catalog of features and their semantics.
- **Backtest harness spec** — how the harness consumes this schema and produces
  the evidence sections.
- **CPCV module spec** — purging, embargo, path generation.
- **Selection layer LLM prompt template** — how `context_conditions` and
  `backtest_evidence` get rendered into the selection prompt.
- **Library governance doc** — roles, review cadence, retirement/promotion gates.
- **Example strategy specs** — 4-6 starter library entries, fully filled in.
