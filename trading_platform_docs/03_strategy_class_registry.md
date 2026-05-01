# Strategy Class Registry

**Status:** Draft for review
**Purpose:** Define the catalog of implementable strategy classes. Classes are the code-level building blocks that library strategies parameterize and compose. The registry is closed — adding a class requires a code change, unit tests, and a registry PR. This is the firewall that prevents novel strategy generation in YAML.

---

## Implementation status (2026-04-28)

The Phase 2A–C vertical slice has shipped:

- **Strategy class ABC + registry mechanics** — `src/tradegy/strategies/base.py`.
  Mirrors the transform / live-adapter pattern (decorator + lookup, fresh
  instance per call). Registered classes:
  - `stand_down` — the trivial first-class option per the section below.
  - `momentum_breakout` — long-only continuation entry on a return-horizon
    feature crossing a threshold.
  - `vwap_reversion` — long-only fade of intraday extension below session
    VWAP. Added in Phase 4 alongside the session-aware harness loop and
    the `mes_vwap` feature it depends on (the hypothesis-driven feature
    addition path documented in `02_feature_pipeline.md` "Feature
    inventory growth").
- **Five auxiliary registries** — `src/tradegy/strategies/auxiliary.py`.
  Each axis has its own ABC + registry generic. Registered classes:
  - Sizing: `fixed_contracts`
  - Stop: `fixed_ticks`
  - Stop adjustment: *(none yet — ABC only; nothing in the MVP spec uses one)*
  - Exit: `time_stop`
  - Condition evaluator: `feature_threshold`

What the docs called for that has NOT been implemented yet (filed as
deferred work, prioritized when a hypothesis spec genuinely needs it —
see the "vital signs vs hypothesis-driven" principle in
`02_feature_pipeline.md`):

- Strategy classes: `range_break_fade`, `range_break_continuation`.
  (Adding either requires range-defining features — range_high/low or
  similar — to be built in the feature pipeline first via the same
  hypothesis-driven path that brought `mes_vwap` in for `vwap_reversion`.)
- Sizing classes: `fixed_fractional_risk`, `volatility_scaled`,
  `kelly_fraction`.
- Stop classes: `opposite_range_extreme`, `atr_multiple`,
  `structural_swing`.
- Stop adjustments: `move_to_breakeven`, `trail_by_atr`,
  `tighten_to_distance`, `step_to_level`.
- Exit classes: `r_multiple_target`, `price_level_target`,
  `invalidation_condition` (general), `end_of_session_flatten`.
- Condition evaluators: `feature_range`, `feature_delta`,
  `calendar_event`, `regime_probability`,
  `no_major_macro_event_within`, `position_pnl_threshold`,
  `bars_since_entry`, `price_returns_to_level`,
  `opposite_direction_break`, `sentiment_shock`.

The contract surface (ABCs, parameter_schema, validate_parameters,
registration) is stable; new entries are mechanical.

---

## What a strategy class is

A **strategy class** is a code-level implementation that:

- Conforms to the registered strategy class interface (see below)
- Accepts a declared parameter contract and validates inputs
- Produces deterministic, replayable behavior given market data and parameters
- Has been unit-tested against its contract
- Is versioned and owned

A **library strategy** is an instantiation of a strategy class with specific parameters, context conditions, and lifecycle configuration. Many library strategies can share one class. A class without library strategies referencing it is *available* but not *in use*.

---

## Class contract (interface)

Every registered class must implement:

```python
class StrategyClass:
    id: str                        # registry ID, e.g. "range_break_fade"
    version: str                   # semver
    parameter_schema: dict         # JSON schema for parameters
    feature_dependencies: list     # feature IDs the class can reference
    invariants: list               # runtime assertions

    def validate_parameters(params: dict) -> ValidationResult:
        """Static validation of parameter values against schema."""

    def initialize(params: dict, instrument: str, session_date: date) -> State:
        """Set up state machine for a session."""

    def on_bar(state: State, bar: Bar, features: FeatureSnapshot) -> list[Action]:
        """Process a new bar. Return zero or more actions (orders, state transitions)."""

    def on_fill(state: State, fill: Fill) -> list[Action]:
        """Process a fill confirmation."""

    def on_exit(state: State, reason: ExitReason) -> list[Action]:
        """Process external exit command (e.g. LLM override)."""

    def current_state(state: State) -> StateSnapshot:
        """Serializable state for logging and replay."""
```

**Required properties:**

- **Determinism.** Given `(params, bar stream, feature stream, fill stream)`, behavior is bit-exact reproducible. No wall-clock dependencies, no unseeded randomness, no network calls.
- **Statelessness at the class level.** All state lives in the `State` object passed between methods. The class itself holds no session state.
- **No LLM access.** Classes cannot call LLMs, external APIs, or any service. They consume features and produce actions. That's it.
- **No feature creation.** Classes consume features from the feature pipeline. They do not compute novel features internally. If a new feature is needed, it's added to the feature pipeline and registered.

---

## Class registration requirements

To add a new class to the registry:

1. **Implementation** conforming to the interface above.
2. **Parameter schema** as JSON Schema. All parameters must have declared types, ranges, and defaults.
3. **Feature dependencies** declared — which feature IDs the class can consume.
4. **Unit test suite** covering:
   - Parameter validation (valid and invalid inputs)
   - Determinism (same inputs → identical outputs across runs)
   - State machine transitions (all expected transitions fire correctly)
   - Edge cases (empty bars, gap data, exit handling)
   - Serialization (state can be serialized and restored)
5. **Invariant assertions** — runtime checks the class enforces about itself.
6. **Registry PR** — reviewed, approved, merged.
7. **Documentation** — class description, intended use, parameter semantics, known limitations.

No class enters the registry without all seven. Classes can enter at `development` status and be promoted to `available_for_library` after additional integration testing.

---

## Class registry schema

```yaml
class_registry_entry:
  id: "range_break_fade"
  version: "1.0.0"
  status: "available_for_library"   # development | available_for_library | deprecated | retired
  description: |
    Fades a breakout of a defined range when volume fails to confirm.
    Enters in opposite direction with stop at opposite range extreme.
  intended_mechanism: |
    Exploits failed breakouts driven by retail momentum without institutional
    follow-through. Weak-volume break signals lack of commitment.
  parameter_schema:
    range_window_minutes:
      type: integer
      min: 10
      max: 120
      default: 30
    break_trigger_ticks:
      type: integer
      min: 1
      max: 10
      default: 2
    volume_confirmation:
      type: object
      properties:
        enabled: {type: boolean, default: true}
        lookback_bars: {type: integer, min: 5, max: 100, default: 20}
        required_multiple: {type: number, min: 0.5, max: 3.0, default: 1.2}
    max_attempts_per_session:
      type: integer
      min: 1
      max: 10
      default: 1
    direction:
      type: enum
      values: [long, short, both]
      default: both
  feature_dependencies:
    required:
      - "bar_ohlcv"
      - "volume_rolling_mean_20bar"
    optional:
      - "vix_level"   # used if context condition references it
  sizing_classes_compatible: ["fixed_fractional_risk", "volatility_scaled"]
  stop_classes_compatible: ["opposite_range_extreme", "atr_multiple"]
  exit_classes_compatible: ["r_multiple_targets", "time_stop", "invalidation_conditions"]
  implementation:
    module: "strategies.range_break_fade"
    class_name: "RangeBreakFade"
    source_hash: "sha256:..."
  tests:
    suite: "tests/strategies/test_range_break_fade.py"
    last_run: "2026-03-15T10:22:00Z"
    coverage_pct: 94
  owner: "dan"
  created: "2026-02-10"
  last_updated: "2026-03-15"
  library_strategies_using: []   # populated by library references
```

---

## Auxiliary class registries

Strategy classes compose with other registered class types. Each has its own registry with parallel structure.

### Sizing class registry

Implementations of position sizing methods. Referenced by strategy specs in the `sizing.method` field.

Initial entries:

- **`fixed_contracts`** — fixed N contracts per trade regardless of risk
- **`fixed_fractional_risk`** — size to risk X% of equity per trade given stop distance
- **`volatility_scaled`** — size inversely proportional to recent realized volatility
- **`kelly_fraction`** — fractional Kelly given estimated edge and variance (advanced; use with strict fraction cap)

### Stop class registry

Implementations of stop placement methods. Referenced in `stops.initial_stop.method`.

Initial entries:

- **`fixed_ticks`** — stop N ticks from entry
- **`opposite_range_extreme`** — stop at opposite side of defined range + buffer
- **`atr_multiple`** — stop at entry ± K × ATR
- **`structural_swing`** — stop beyond recent swing high/low

### Stop adjustment class registry

Implementations of dynamic stop modification. Referenced in `stops.adjustment_rules[].action`.

Initial entries:

- **`move_to_breakeven`** — move stop to entry (+ offset)
- **`trail_by_atr`** — trail stop at K × ATR from recent extreme
- **`tighten_to_distance`** — reduce stop distance to fixed value
- **`step_to_level`** — move stop to specific price level

### Exit class registry

Implementations of profit-taking and exit logic. Referenced in `exits.profit_targets` and `exits.invalidation_conditions`.

Initial entries:

- **`r_multiple_target`** — take partial at N×R
- **`price_level_target`** — take at absolute price
- **`time_stop`** — exit at bar count or clock time
- **`invalidation_condition`** — exit when condition evaluator returns true
- **`end_of_session_flatten`** — exit N minutes before close

### Condition evaluator registry

Registered, composable conditions referenced in `entry.gating_conditions` (harness-enforced pre-entry gates), `context_conditions.structured` (LLM-readable prose), and `exits.invalidation_conditions` (post-entry flatten triggers). Conditions evaluate against the feature stream and return boolean.

Initial entries (✅ implemented, otherwise planned):

- ✅ **`feature_threshold`** — `feature X > threshold Y` (operators: gt | gte | lt | lte | eq)
- ✅ **`feature_range`** — `feature X in [lo, hi]` (inclusive bounds; either lo, hi, or both)
- ✅ **`time_of_session`** — `session_position in [lo, hi]` (canonical time-of-day gate, sugar over `feature_range` keyed on `mes_session_position`)
- **`feature_delta`** — change in feature X over window Y > threshold Z
- **`calendar_event`** — within N minutes of declared event type
- **`regime_probability`** — regime probability > threshold
- **`no_major_macro_event_within`** — no declared high-impact event in window
- **`position_pnl_threshold`** — open position has gained/lost X R
- **`bars_since_entry`** — N bars have elapsed since entry
- **`price_returns_to_level`** — price has touched specified level
- **`opposite_direction_break`** — price has broken specified level in opposite direction with volume
- **`sentiment_shock`** — sentiment feature delta exceeds threshold (only usable once a sentiment source has been admitted against the universal bar in the feature pipeline)

Conditions can be composed with `and`, `or`, `not` in the spec. The harness applies `entry.gating_conditions[]` as an implicit AND — all must be True for the strategy class's `on_bar` to be invoked.

---

## Adding to registries

All auxiliary registries follow the same rules as the strategy class registry:

- Code implementation conforming to registered interface
- Parameter schema
- Unit tests
- Registry PR with review
- Deprecation via explicit lifecycle, not silent removal

Deprecated classes remain available for existing library strategies until those strategies are migrated or retired. No breaking changes without migration.

---

## Initial class catalog for library build-out

For the starting library of 4–6 strategies, the minimum set of classes to implement:

**Strategy classes (Phase 1):**

- `range_break_fade` — fades failed breakouts of a defined range
- `range_break_continuation` — enters on confirmed range break with volume
- `vwap_reversion` — fades deviations from VWAP in range-bound sessions
- `stand_down` — trivial "do nothing" class; first-class selectable option for the LLM

**Sizing classes:** `fixed_contracts`, `fixed_fractional_risk`

**Stop classes:** `fixed_ticks`, `opposite_range_extreme`, `atr_multiple`

**Stop adjustments:** `move_to_breakeven`, `trail_by_atr`

**Exit classes:** `r_multiple_target`, `time_stop`, `invalidation_condition`, `end_of_session_flatten`

**Condition evaluators:** `feature_threshold`, `feature_range`, `calendar_event`, `regime_probability`, `no_major_macro_event_within`, `position_pnl_threshold`, `bars_since_entry`, `price_returns_to_level`, `opposite_direction_break`

That's roughly 25 registered implementations. Each needs:
- Code implementation
- Parameter schema
- Unit test suite
- Documentation

Estimated effort: several weeks for a single developer. This is the mechanical build-out that has to exist before the strategy pipeline can produce its first validated library entry.

---

## The `stand_down` class

Worth calling out specifically because it's unusual. `stand_down` is a strategy class whose behavior is "do nothing" — it never enters ARMED, never fires any trigger, never produces any order. It exists so the selection layer can pick "stand down today" as a first-class option, not as absence-of-selection.

Why this matters: the LLM selection layer benefits from explicit rather than implicit options. "Pick `stand_down`" is a concrete, reasoned decision with its own rationale. "Pick nothing" is harder to log, harder to audit, and more susceptible to drift toward over-activation.

---

## What the registry deliberately does not contain

- **Composite classes** that combine multiple strategies. Composition happens at the library-spec level, not the class level. If a library strategy wants ORB entry logic with VWAP exit logic, that's either two strategies running with coordination or (more likely) rejected as too complex for v1.
- **Learning classes.** No class updates its own parameters based on live performance. All parameter updates happen through library governance.
- **Meta-classes.** No class that selects among other classes. Selection is the LLM's job, not a class's job.
- **Novel architectures.** Every class is a known trading pattern. Exotic approaches go through research before becoming registry candidates.

---

## Relationship to the spec schema

The strategy spec schema (`04_strategy_spec_schema.md`) references this registry through:

- `entry.strategy_class` — must resolve to a registered class
- `sizing.method` — must resolve to a registered sizing class
- `stops.initial_stop.method` — must resolve to a registered stop class
- `stops.adjustment_rules[].action` — must resolve to a registered adjustment class
- `exits.profit_targets[].method` — must resolve to a registered exit class
- `exits.invalidation_conditions[]` — must resolve to registered condition evaluators
- `context_conditions.structured.required_conditions[]` — must resolve to registered condition evaluators

The harness validates every reference at spec load time. Unresolvable references abort the load. This is the mechanism that keeps specs honest.

---

## Versioning and migration

Class versions follow semver:

- **PATCH** — bug fixes, no behavior change. Backward compatible. Library strategies auto-upgrade.
- **MINOR** — added parameters (with defaults), expanded ranges. Backward compatible. Library strategies auto-upgrade; new parameters available but not required.
- **MAJOR** — parameter contract change, behavior change. **Not backward compatible.** Library strategies pin to the old version; migration is explicit and requires revalidation.

A class can have multiple major versions live simultaneously. Library strategies reference a specific `class_id@version`. This is essential — a bug fix in v2 shouldn't silently change the behavior of a library strategy that was validated against v1.

---

## Open design decisions

1. **Should classes declare compatibility with specific sizing/stop/exit combinations, or is compatibility determined at spec validation time?** Current schema has compatibility lists. Alternative: always allow any combination and catch incompatibilities at validation. Leaning toward declared compatibility for clarity.

2. **Multi-instrument classes.** Currently all classes are ES-specific in behavior. When we add NQ, do we generalize classes to be instrument-parameterized, or fork class-per-instrument? Leaning generalize, with instrument-specific parameter defaults.

3. **Class performance telemetry.** Should the registry track aggregate performance across all library strategies using each class? Useful for "this class's implementations tend to underperform their envelopes" signals. Probably add in Phase 2.

4. **Class deprecation triggers.** What causes a class to move from `available_for_library` to `deprecated`? Currently undefined. Propose: no new library strategies can use a deprecated class, existing ones must migrate within N months, class is retired when unused.
