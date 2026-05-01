# Backtest Harness Spec

**Status:** Draft for review
**Purpose:** Define the deterministic, tick-level backtest engine that consumes strategy specs and feature registry data and produces signed, reproducible evidence. This is the mechanism that turns strategy specs into the `backtest_evidence` and `validation_record` sections of each library entry.

---

## Implementation status (2026-04-30)

Phase 3A shipped the MVP single-spec single-window driver in
`src/tradegy/harness/`:

| Component | Status |
|---|---|
| Spec loader / validator | implemented (delegates to `tradegy.specs.loader`) |
| Bar stream loader | implemented (canonical `{instrument}_1m_bars` feature) |
| Feature stream loader | implemented (join_asof on `served_at`, honors availability_latency) |
| Time-controlled clock | implemented as bar iterator (no future-data access by construction) |
| Session-aware loop reset | **implemented (Phase 4A)** — bars tagged with CMES session id; force-flatten with ExitReason.SESSION_END at each boundary; strategy state reinitialized so per-session counters reset; bars outside any session skipped |
| Strategy state machine driver | implemented |
| Order lifecycle: market | implemented (next-bar-open + fixed-tick adverse slippage) |
| Order lifecycle: stop | implemented (fill at stop + adverse slippage when bar trades through) |
| Order lifecycle: limit | NOT implemented (defer until a strategy spec needs limit entries) |
| Slippage model | fixed-tick per side (the simple v1 alternative described in the doc) |
| Commission model | per-contract round-trip, configurable |
| Margin cost | NOT implemented (no overnight holding cost in v1) |
| Position tracker | implemented |
| Per-trade record | implemented (Trade dataclass) |
| Aggregate stats | implemented: total_trades, expectancy_R, total_pnl, win_rate, avg_win_R, avg_loss_R, profit_factor, avg_holding_bars, per-trade Sharpe, max_drawdown |
| Regime-stratified stats | NOT implemented |
| Parameter sensitivity sweep | NOT implemented |
| Baseline comparisons | NOT implemented |
| Walk-forward | **implemented (Phase 5A)** — rolling (train, test) windows; same parameters in both halves; gate per `07_auto_generation.md:171` (avg OOS Sharpe ≥ 50% of avg in-sample, in-sample must be positive) |
| Walk-forward parameter optimization | NOT implemented (within-envelope grid search; would compound multi-testing problem and needs Deflated Sharpe correction) |
| CPCV | **implemented (Phase 6A)** — combinatorial paths over `N` equal-width folds with `k` test folds per path (`C(N, k)` paths); per-path trades concatenated and aggregated; cross-path distribution reports median Sharpe, IQR, pct-paths-negative; gate per doc 05:343 (configurable). `purge_days` / `embargo_days` are accepted but no-op in this MVP — they activate when within-train fitting is added |
| Stress periods | NOT implemented |
| Leakage audit (recompute features at sampled T) | covered already by `tradegy validate <feature>` at the feature-pipeline layer; harness-side audit deferred |
| Evidence signing | NOT implemented |
| Run modes | `single`, `walk_forward`, and `cpcv` shipped (CLI: `tradegy backtest`, `tradegy walk-forward`, `tradegy cpcv`); `sensitivity` / `variant_sweep` / `regression` / `batch` deferred |
| Multi-strategy simulation | NOT implemented |
| Live replay drift detection | NOT implemented |

CLI: `tradegy backtest <spec_id>` runs a `single` mode backtest and
prints aggregate stats. `tradegy walk-forward <spec_id>` runs rolling
(train, test) walk-forward and prints per-window + aggregate stats.
`tradegy cpcv <spec_id>` runs combinatorial purged CV with configurable
`--n-folds` / `--k-test-folds` and prints the per-path Sharpe table
plus the distribution gate. End-to-end runs on real MES data (2019-05
→ 2025-06, 609,923 1m bars):

| Spec | Bar source | Trades | Expectancy R | Walk-forward gate | Notes |
|---|---|---|---|---|---|
| `mes_momentum_breakout` | mes_5s_ohlcv (partial-day) | 9,719 | -0.29 | FAIL | naive momentum continuation |
| `mes_vwap_reversion` | mes_5s_ohlcv (partial-day) | 1,512 | -0.56 | FAIL (avg IS Sharpe -0.31, OOS -0.26) | naive long-only fade |
| `mes_vwap_reversion_gated` (H2) | mes_5s_ohlcv (partial-day) | 565 | +0.0513 | FAIL (gate ratio -2.58) | gates raise IS Sharpe but OOS still negative |
| `mes_vwap_reversion_gated` (H2 re-test) | mes_1m_ohlcv (24h) | 1,486 | -0.659 | n/a (FAIL sanity, IS Sharpe -0.380) | re-test on full-coverage data; "barely positive" prior result was an artifact of partial-coverage filtering out morning RTH |
| `mes_orb_failure_fade` (H1) | mes_1m_ohlcv (24h) | 2,718 | -0.526 | n/a (FAIL sanity, IS Sharpe -0.238) | range-break fade with 12-tick fixed stop; trigger fires too easily on minor wicks |
| `mes_orb_continuation` (H3) | mes_1m_ohlcv (24h) | 3,000 | -0.598 | n/a (FAIL sanity, IS Sharpe -0.386) | inverse mechanism of H1; volume-confirmed range break + 12-tick fixed stop |

**Round-2 sprint outcome (2026-04-30):** All three pre-registered hypotheses (H1, H2 re-test, H3) failed the sanity gate (raw IS Sharpe ≤ 0). Hypothesis budget exhausted (3/3). Common failure mode: ~20-23% win rate with avg_loss near full stop, indicating the fixed-tick stop framework + ~2.2-tick cost overhead per round trip eats the asymmetric R/R distribution. The 12-tick stop is too tight relative to MES intraday true-range. Per sprint anti-overfitting rules, no parameter tuning permitted.

**Round-3 sprint outcome (2026-05-01):** Three new hypotheses (gap-fill fade, compression breakout, volume-spike fade) with 3 pre-registered variants each (9 total, within the 12-variant budget). All used the new `atr_multiple` stop class to test whether wider ATR-scaled stops fix the round-2 failure mode. **All 9/9 variants killed at sanity.** Best raw result: `mes_gap_fill_a` at -0.174 Sharpe. The compression and volume-spike variants fired 3500-3600 times each over 7 years, indicating selectivity — not stop sizing — is the binding constraint. ATR-multiple stops shifted avg_loss from ~-1.3R (round 2) to ~-2.0R (round 3) without lifting win rates. See `06_hypothesis_system.md` § Cross-sprint synthesis for the full record.

| Round 3 spec | Trades | IS Sharpe | Sanity |
|---|---|---|---|
| `mes_gap_fill_a` (0.3% gap) | 1453 | -0.174 | FAIL |
| `mes_gap_fill_b` (0.5%) | 1080 | -0.205 | FAIL |
| `mes_gap_fill_c` (1.0%) | 481 | -0.329 | FAIL |
| `mes_compression_breakout_a` (TR<0.3×ATR) | — | ERROR | ATR cap exceeded 2020-03-16 (peak COVID vol) |
| `mes_compression_breakout_b` (TR<0.4) | 3602 | -0.691 | FAIL |
| `mes_compression_breakout_c` (TR<0.5) | 3604 | -0.661 | FAIL |
| `mes_volume_spike_fade_a` (z≥2.0) | 3593 | -0.658 | FAIL |
| `mes_volume_spike_fade_b` (z≥2.5) | 3537 | -0.633 | FAIL |
| `mes_volume_spike_fade_c` (z≥3.0) | 3261 | -0.622 | FAIL |

---

## Design principles

1. **Bit-exact reproducibility.** Running the same spec against the same data with the same harness version must produce identical results every time. No wall-clock dependencies, no unseeded randomness, no environment leakage.

2. **Point-in-time fidelity.** At every simulated moment T, the strategy can only see data that was actually available at T + availability_latency. Enforced mechanically, not by convention.

3. **Live-path parity.** The code that executes a strategy in backtest is the same code that executes it in live. No separate "backtest simulator" — the harness wraps the live engine with a time-controlled data source and a simulated execution layer.

4. **Honest costs.** Commissions, slippage, margin costs, data fees all modeled. Gross-positive, net-negative strategies are flagged, not hidden.

5. **Signed outputs.** The harness signs its output so that humans cannot edit stats after the fact. Any modification invalidates the signature.

6. **Versioned.** Harness itself is semver-versioned. Every backtest result records the exact harness version used. Regression testing against the harness is first-class.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ BACKTEST HARNESS                                            │
│                                                             │
│  ┌──────────────────┐     ┌──────────────────────────┐     │
│  │ Spec Loader      │────▶│ Spec Validator           │     │
│  │  - YAML parse    │     │  - Schema check          │     │
│  │  - Class refs    │     │  - Envelope check        │     │
│  │  - Feature refs  │     │  - Registry refs         │     │
│  └──────────────────┘     └──────────┬───────────────┘     │
│                                      │                      │
│                                      ▼                      │
│  ┌──────────────────┐     ┌──────────────────────────┐     │
│  │ Data Source      │────▶│ Time-Controlled Clock    │     │
│  │  - Tick stream   │     │  - Advances one event    │     │
│  │  - Feature stream│     │    at a time             │     │
│  │  - Event calendar│     │  - Enforces availability │     │
│  └──────────────────┘     │    latency               │     │
│                           └──────────┬───────────────┘     │
│                                      │                      │
│                                      ▼                      │
│  ┌──────────────────────────────────────────────────┐      │
│  │ Strategy Execution Engine                        │      │
│  │  (same engine as live)                           │      │
│  │  - State machine per active strategy             │      │
│  │  - Consumes features, emits actions              │      │
│  └──────────┬───────────────────────────────────────┘      │
│             │                                               │
│             ▼                                               │
│  ┌──────────────────────────────────────────────────┐      │
│  │ Simulated Execution Layer                        │      │
│  │  - Order lifecycle                               │      │
│  │  - Fill simulation (slippage model)              │      │
│  │  - Position tracking                             │      │
│  │  - Cost accounting                               │      │
│  └──────────┬───────────────────────────────────────┘      │
│             │                                               │
│             ▼                                               │
│  ┌──────────────────────────────────────────────────┐      │
│  │ Result Aggregator                                │      │
│  │  - Per-trade records                             │      │
│  │  - Aggregate stats                               │      │
│  │  - Regime-stratified stats                       │      │
│  │  - Evidence packet with signature                │      │
│  └──────────────────────────────────────────────────┘      │
└─────────────────────────────────────────────────────────────┘
```

---

## Core invariants the harness enforces

**Availability latency.** For every feature referenced at simulated time T, the harness serves the value that was available at T minus the feature's declared availability latency. Attempting to access a feature value closer to T than its latency allows raises an error.

**No future data.** The clock advances monotonically. Strategies cannot query data at time T' > current_T. The API makes this structurally impossible, not merely discouraged.

**Deterministic order handling.** Orders submitted in a bar are processed in a fixed, documented sequence. Same bar = same handling = same result.

**Fill realism.** Limit orders fill only if price actually traded through the limit. Market orders fill at simulated slippage-adjusted price. Stop orders fill when triggered, with slippage. No magic fills.

**Cost application.** Every trade incurs modeled commissions and slippage. Holding positions overnight incurs modeled margin cost. Results report both gross and net figures.

---

## Spec validation (pre-run)

Before any backtest runs, the harness validates the spec:

1. **Schema compliance** — conforms to `04_strategy_spec_schema.md`.
2. **Registry resolution** — every referenced class (strategy, sizing, stop, exit, condition) resolves to an available implementation.
3. **Parameter envelope** — every current parameter value lies within its declared tested envelope.
4. **Feature availability** — every feature the strategy depends on exists in the feature registry with sufficient historical coverage for the requested backtest window.
5. **Revisability inheritance** — records whether any feature dependency is revisable; if so, the backtest run pins source vintages as well as feature versions, and results are tagged accordingly.
6. **Invariant checks** — `hard_max_distance_ticks` respected, `max_daily_loss` within portfolio limits, auto-disable triggers declared.

Validation failure aborts the run with structured error. No partial validation — all or none.

---

## Data source abstraction

The harness consumes:

- **Bar stream** — OHLCV at declared resolution (tick, 1-sec aggregated, 1-min, 5-min, etc.), from the canonical data lake.
- **Feature stream** — per-bar feature values from the feature registry, with availability latencies pre-applied.
- **Event calendar** — scheduled events (FOMC, CPI, etc.) with exact timestamps, for blackout enforcement and context.
- **Corporate actions / session metadata** — market hours, holidays, contract rolls.

All consumed via the registry API. The harness does not read raw data files directly; it goes through the registered interface. This ensures backtest fidelity matches production.

---

## Simulated execution layer

### Order lifecycle

- **Limit orders.** Rest on simulated book at the specified price. Fill if and only if a bar trades through the limit price (high ≥ limit for buys, low ≤ limit for sells). No partial fills in v1 (single contract; for multi-contract MES, could add partial fill simulation later).
- **Market orders.** Fill at the next bar's open + slippage adjustment.
- **Stop orders.** Trigger when price crosses stop. Fill at triggered price + slippage.
- **Stop-limit orders.** Trigger like stop; rest as limit from there.
- **Cancellations.** Effective at next bar boundary unless specified otherwise.

### Slippage model

Base model: **cost-aware per-side slippage** proportional to volatility and inverse to displayed volume. Default parameters:

```
slippage_ticks = max(min_slippage, k * recent_realized_vol / normalized_volume)
```

Simpler alternative for v1: **fixed per-side slippage** (e.g., 0.5 ticks per side), configurable per instrument. Accept the crudeness; iterate later.

Both models produce slippage in ticks, applied adversely (worse fill for the trader) to every execution. Limit-order fills get zero slippage but require price to trade through.

### Commission model

Per-contract round-trip commission, configurable. Default for MES at IB: approximately $1.50 round trip (subject to verification against current IB schedule). Applied per fill, accumulated per trade.

### Margin cost

Overnight-held positions incur modeled margin cost at declared annual rate. Computed per position, per overnight period.

### Fill reporting

Every fill produces a structured record:

```yaml
fill:
  trade_id: "..."
  timestamp: "..."
  strategy_id: "..."
  side: "buy|sell"
  quantity: ...
  price: ...
  slippage_ticks: ...
  commission: ...
  remaining_position: ...
```

Fill records feed both position tracking and the final trade log.

---

## Result aggregation

### Per-trade records

Every completed round trip produces:

```yaml
trade:
  trade_id: "..."
  strategy_id: "..."
  strategy_version: "..."
  instrument: "..."
  entry_time: "..."
  exit_time: "..."
  entry_price: ...
  exit_price: ...
  side: "long|short"
  size: ...
  gross_pnl: ...
  commissions: ...
  slippage_cost: ...
  net_pnl: ...
  net_pnl_R: ...               # in initial-risk multiples
  initial_stop: ...
  initial_risk_ticks: ...
  actual_risk_taken_ticks: ... # if stop was moved
  holding_duration: ...
  exit_reason: "target|stop|invalidation|time|session_end|override"
  regime_at_entry: {...}
  features_at_entry: {...}     # sampled feature values for attribution
```

### Aggregate stats (post-cost)

```yaml
aggregate:
  total_trades: ...
  expectancy_R: ...
  total_pnl_R: ...
  sharpe: ...
  sortino: ...
  max_drawdown_pct: ...
  max_drawdown_duration_days: ...
  win_rate: ...
  avg_win_R: ...
  avg_loss_R: ...
  profit_factor: ...
  avg_holding_time_minutes: ...
  worst_month_R: ...
  best_month_R: ...
  consecutive_losses_max: ...
  consecutive_wins_max: ...
```

### Regime-stratified stats

Aggregate stats recomputed within each regime bucket:

```yaml
regime_stratified:
  range_bound:
    trades: ...
    sharpe: ...
    expectancy_R: ...
    win_rate: ...
  trending:
    trades: ...
    ...
  high_vol:
    ...
  low_vol:
    ...
```

Regime buckets are defined by the HMM regime model's output (once available) or by simpler heuristic regime classifiers in the meantime. Regime bucketing itself is a registered feature, so different regime definitions can be swapped without changing the harness.

### Parameter sensitivity

Run the strategy across the ±20% perturbation grid of each numeric parameter. Output:

```yaml
sensitivity:
  param_grid: [...]
  sharpe_surface: [...]
  worst_perturbation_sharpe_delta: ...
  passed: true   # no cliffs, defined as max delta < 0.5 within ±20%
```

### Baseline comparisons

Run against pre-defined baselines over the same data window:

- Buy-and-hold the instrument
- Random entry with matched holding time distribution
- Simple regime-mapped rule (e.g., long on strong-trend days, short on weak-trend days)

Output the Sharpe of each baseline alongside the strategy's Sharpe for context.

---

## Validation suite

The validation suite wraps the harness to produce the evidence needed for library promotion.

### Walk-forward

- Configurable window: e.g., 3-year train, 1-year test, roll forward 1 year at a time.
- Parameters may be tuned within the train window (within declared envelope) and evaluated on test.
- Output: per-window Sharpe, aggregate out-of-sample Sharpe, in-sample vs out-of-sample divergence.

### Combinatorial Purged Cross-Validation (CPCV)

- Divide data into N folds (default N=10).
- Generate multiple backtest paths by assigning folds to train vs test in different combinations.
- **Purge** a buffer of bars around each test fold to eliminate label leakage.
- **Embargo** a post-test period from future training.
- Run the strategy on each path.
- Output: distribution of Sharpes across paths, median, IQR, pct paths negative.

Gate: configurable — commonly "median Sharpe > 0.8 AND pct paths negative < 20%".

### Leakage audit

- For a random sample of historical timestamps, recompute the feature values the strategy saw using only pre-T data.
- Compare against the feature values actually served by the harness.
- Any discrepancy is a bug (in harness or feature pipeline) and aborts the validation.

### Stress periods

Run the strategy through pre-defined historical stress windows:

- March 2020 (COVID crash)
- August 2015 (flash crash)
- February 2018 (vol spike)
- Late 2022 (rate shock)

Output: per-stress-period stats. Used to populate `known_failure_modes`.

---

## Evidence signing

Each harness run produces an evidence packet. The packet is deterministic in:

- Harness version
- Spec content (hash)
- Input data versions (hash)
- Random seed (where applicable)

The packet is signed via:

```
signature = sha256(harness_version || spec_hash || data_versions_hash || seed || stats_canonical_json)
```

The `backtest_evidence.harness_signature` field in the strategy spec is this signature. Any human edit to the stats breaks the signature. The spec loader refuses to load specs whose signatures don't verify.

---

## Reproducibility contract

**Same inputs → same outputs, forever.**

"Same inputs" means:
- Same spec version
- Same data versions (data source + feature registry entries)
- Same harness version
- Same random seed (where randomness is involved)

Re-running a backtest with identical inputs and getting different outputs is a harness bug. The regression test suite includes "golden path" runs: known specs against known data with expected outputs. Any change to the harness must produce identical outputs for golden path runs, or the change is a breaking version bump with explicit migration.

---

## Failure modes the harness is designed to prevent

- **Lookahead via feature peek.** Architecturally prevented by time-controlled clock.
- **Optimistic fills.** Prevented by requiring price to trade through limits, slippage on all market/stop orders.
- **Cost omission.** Prevented by mandatory cost model; no way to run "costless" mode for final evidence.
- **Silent data revisions.** Prevented by feature version pinning at run time.
- **Parameter creep.** Prevented by envelope validation.
- **Result tampering.** Prevented by signing.
- **Irreproducibility.** Prevented by full seed and version logging.

---

## Performance requirements

Target: **one year of ES tick data, single strategy, full backtest, under 10 minutes on commodity hardware.** This is not HFT-grade but comfortably allows iterative development. CPCV with 100 paths should complete overnight.

Optimization approach: columnar data access (Parquet), vectorized feature lookups, strategy state machines written in typed Python (or hot paths in Rust/C++ if needed). Premature optimization avoided in v1.

---

## Run modes

The harness supports multiple execution modes:

- **`single`** — one spec, one window, full report.
- **`walk_forward`** — one spec, rolling windows, walk-forward report.
- **`cpcv`** — one spec, N paths, distribution report.
- **`sensitivity`** — one spec, parameter grid, sensitivity report.
- **`variant_sweep`** — multiple spec variants (from auto-generator), comparative report. Multi-hypothesis correction applied. See `07_auto_generation.md`.
- **`regression`** — golden path replay for harness validation.
- **`batch`** — multiple specs, shared data load, parallel execution. For library-wide re-evaluations on new data.

Each mode produces a structured result artifact and logs all intermediate state for replay.

---

## Integration with other pipelines

**Consumes from feature pipeline:**
- Registered data sources (via registry API)
- Registered features (via registry API, at specific versions)
- Registered models' predictions (via registered features)

**Consumes from strategy pipeline:**
- Strategy specs (human-authored or auto-generated)
- Strategy class registry (via runtime resolution)

**Produces for strategy pipeline:**
- `backtest_evidence` sections
- `validation_record` sections (CPCV, walk-forward, stress)
- Signed harness signatures

**Produces for governance:**
- Regression test results (on every harness change)
- Library-wide reperformance reports (on schedule)
- Drift analyses (backtest vs live divergence for live-tier strategies)

---

## Open design decisions

1. **Tick-level vs bar-level backtesting.** Tick-level is honest but expensive. Bar-level is fast but misses intrabar dynamics (especially relevant for stops). Propose: tick-level for final validation, bar-level allowed for early iteration.

2. **Slippage model sophistication.** Fixed per-side (simple) vs volatility-scaled (more realistic) vs modeled from actual spread/volume at the bar (most realistic, most data-hungry). Start simple; upgrade when we have the data.

3. **Multi-strategy simulation.** Can the harness run multiple strategies simultaneously with realistic portfolio-level risk and incompatibility constraints? Needed eventually for realistic library-wide evaluation. Not needed for single-strategy validation. Propose: build in v1.1, single-strategy first.

4. **Live replay for drift detection.** Should the harness replay live decisions against the same data to detect divergence between backtest and live? This is how we catch subtle bugs where live diverges from backtest. Probably yes, as a scheduled process.

5. **Warm-up period handling.** Strategies may require N bars of history before their signals are reliable. Current proposal: each strategy declares `minimum_warm_up_bars` and the harness skips that period. Needs to be explicit in the spec schema.

6. **Gap handling.** Overnight gaps, holiday gaps, missing data gaps. Currently: skip gaps, resume on next available bar. But some strategies explicitly trade gaps. Need a clearer semantic for "what does a gap look like to a strategy."
