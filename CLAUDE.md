# tradegy — project pin

This file is the entry-point briefing for Claude. Read it first; it tells you
where the system is, what's shipped, what's parked, and which rules are
load-bearing. Authoritative specs live in `trading_platform_docs/` (numbered
`00`–`13`) — this file points at them, it does not replace them.

Last pinned: 2026-05-02 (after PR #7 merge, before Round 4 data acquisition).

---

## What this is

Quant research platform for futures (currently MES, with planned VIX and
macro-event inputs). End-to-end: data ingest → feature pipeline → strategy
specs → backtest harness (single, walk-forward, CPCV, holdout) → auto-test
orchestrator → execution layer (IBKR) → live monitoring → governance.

The hard constraint underneath everything is **anti-overfitting discipline**:
pre-registration, variant budgets, honest kill-recording, evidence packets,
no post-hoc tuning. See "Discipline" below — this is non-negotiable.

---

## Repo layout

- `src/tradegy/` — Python package. CLI entry is `tradegy` (Typer); see `cli.py`.
  - `options/` — vol-selling workstream (Phase A): `chain.py`
    (ChainSnapshot/OptionLeg dataclasses), `greeks.py` (Black-Scholes
    pricing + Greeks + IV solver, vendor-independent), `chain_io.py`
    (parquet → typed snapshot reader). Per doc 14.
  - `ingest/csv_orats.py` — ORATS Pro /datav2/hist/strikes CSV ingest;
    canonicalizes vendor camelCase → snake_case, writes date-
    partitioned parquet under `data/raw/source=spx_options_chain/`.
- `registries/` — YAML registries for `data_sources/` and `features/`.
- `strategies/` — strategy spec YAMLs (per `04_strategy_spec_schema.md`).
- `hypotheses/` — hypothesis YAMLs (per `06_hypothesis_system.md`).
- `data/` — runtime artifacts. **All subdirs gitignored except `.gitkeep`.**
  - `data/raw/`, `data/features/` — parquet outputs (Stage 1, Stage 4/7).
  - `data/audits/`, `data/evidence/` — basic audit outputs, signed evidence.
  - `data/auto_generation/<hyp_id>/` — auto-test variant records (JSONL).
  - `data/feature_stats/<feature_id>.json` — distribution sidecars (auto-gen Phase C).
- `tests/` — pytest suite, currently **372 tests**.
- `trading_platform_docs/` — design docs `00`–`13`, README index. Authoritative.
- `/Users/dan/code/data/` — **separate repo** for vendor download scripts
  (databento, ORATS, etc.). Cost-check-then-stop pattern for pay-per-pull
  vendors (databento); ETA-then-stop for flat-rate vendors (ORATS). Every
  script must require `--confirm` before downloading.

## Stack

- Python 3.11+, dependency manager: **`uv`** (run anything as `uv run <cmd>`).
- Data: **`polars`** (not pandas — performance + lazy joins for asof).
- Schemas: **`pydantic v2`**, strict (`extra="forbid"`).
- CLI: **`typer`** + `rich`.
- LLM: **Anthropic SDK** (`anthropic` package). Default model `claude-opus-4-7`,
  adaptive thinking. See `src/tradegy/auto_generation/anthropic_*.py`.
- Tests: **`pytest`**.
- Lint/format: not currently wired (no ruff/black config in repo).

## Run anything

```bash
uv run tradegy --help                 # CLI surface
uv run pytest                         # 372 tests
uv run tradegy registry features      # list registered features
uv run tradegy backtest <spec_id>     # single-window
uv run tradegy walk-forward <spec_id> # rolling
uv run tradegy cpcv <spec_id>         # combinatorial purged CV
uv run tradegy hypothesize --n 3      # LLM hypothesis batch
uv run tradegy auto-vary <hyp_id> --n 4   # LLM variant generation
uv run tradegy auto-test <hyp_id>     # orchestrator: sanity → walk-forward
uv run tradegy refresh-feature-stats  # rebuild distribution sidecars
```

---

## Where the system stands (by doc)

`trading_platform_docs/` is the authoritative ledger. Each doc has a status
table; that is the source of truth, not this summary. The summary below is a
quick-orient:

| Doc | Topic | Status |
|---|---|---|
| 00 | Master architecture | stable |
| 01 | Strategy definition | stable |
| 02 | Feature pipeline | stable; Stage 7 distribution-stats sidecar landed 2026-05-02 |
| 03 | Strategy class registry | classes shipped: `compression_breakout`, `gap_fill_fade`, `momentum_breakout`, `range_break_continuation`, `range_break_fade`, `stand_down`, `volume_spike_fade`, `vwap_reversion` |
| 04 | Spec schema | stable |
| 05 | Backtest harness | Phases 3A/4/5/6A done — single, session-aware, walk-forward, CPCV |
| 06 | Hypothesis system | manual hypothesis lifecycle done; **five-test triage scorer pending**; scanner Phase 1 (kill-record + in-data market-structure observer) landed 2026-05-02 |
| 07 | Auto-generation | Phases A+B+C **shipped and verified**; scanner Phase 1 landed 2026-05-02; holdout in auto-test landed 2026-05-02; Phase C-pending: embedding diversity, Deflated Sharpe Ratio, triage scorer integration |
| 08 | Development pipeline | docs only |
| 09 | Selection layer | docs only |
| 10 | Review / gap analysis | superseded by P0 doc sprint |
| 11 | Execution layer | Phases 1+2+3A+3B+3C **fully shipped** — FSM, idempotency, transition log, risk caps, kill-switch, session-flatten, IBKR router, divergence detector, reconciliation loop |
| 12 | Live monitoring | Phase 1 shipped (framework + `broker_connectivity`, `data_freshness`, `time_skew`, `process_liveness`); **Phase 2+ pending** |
| 13 | Governance | docs only |
| 14 | Options vol selling | scope 2026-05-02; **Phases A+B+C+D complete 2026-05-03**. Full 6-year SPX (1508 days, 2020-2025) ingested. 8 strategy classes + IV-gated wrapper + 4 width-anchored variants = 13 effective configurations. **Multi-year winner: PutCreditSpread bare** (+18% RoC over 6 years, 5/6 positive years, Sharpe 0.34). 2025-only findings (PCS+IV<0.30 winner, IC+width+IV "perfect 5/5") did NOT survive multi-year — regime-local artifacts. IronCondor is natural regime hedge for PCS (positive in 2022 bear when PCS lost). 573 tests passing. Live IBKR contract qualification verified on paper account DU7535411. Next: Phase E paper-trade integration. |

---

## What was attempted and killed

**12 variants killed at sanity** across rounds 1–3 (single-instrument
MES-only, price-derived features). Cross-sprint synthesis in
`06_hypothesis_system.md`: **selectivity is the binding constraint**, not
stop sizing. Round-3 single-feature triggers fired 1,000–3,600 times over
7 years; closest-to-edge mechanism (gap-fill at -0.17 Sharpe) still failed
sanity. **Conclusion**: single-instrument single-domain price streams are
exhausted for this approach.

**Auto-gen verification 2026-05-02**: pre-Phase-C, all 4 LLM-generated
variants picked thresholds outside [p10, p90] and produced 0 trades. Post
Phase-C (feature-stat injection), variant_b on `hyp_midday_compression…`
produced **1,397 trades** with thresholds anchored inside `[p10, p90]`.
Stats injection is verified working. The midday-compression hypothesis
itself is a kill (variant_b -1.018 per-trade Sharpe) — separate problem
from the threshold-grounding fix.

---

## What's next: Round 4 (parked, plan written)

Plan file: `/Users/dan/.claude/plans/pull-latest-main-review-rippling-unicorn.md`
(read it in full before starting work — this is a summary).

Round 4 admits **two cross-domain inputs** (VIX, scheduled macro events) and
encodes a **9-spec batch** where every spec carries ≥2 independent gating
conditions. Directly attacks the selectivity bottleneck.

Phase 0 (data acquisition) has been **partially executed and is blocked**:

- `/Users/dan/code/data/download_vix.py` exists; cost-check returned no
  CFE/VX dataset accessible via the current databento API key.
  `VX.FUT` on `GLBX.MDP3` returns `422 symbology_invalid_request`.
- `/Users/dan/code/data/download_econ_events.py` exists; databento has no
  econ-events feed for this API key. Fallback: hand-curated FRED/BLS CSV.

**Three options on the table**, awaiting user decision:

1. **Drop VIX entirely**, ship N1 (pre-FOMC drift) and N3 (purified gap-fill
   with macro-event quiet-period gate). Two of three hypotheses survive.
2. **Daily cash-VIX from a free source** (CBOE/Yahoo), broadcast forward via
   `cross_asset_close_aligned`. All three hypotheses ship; N2 (VIX-spike-fade)
   becomes daily-regime-based not intraday-divergence-based.
3. **Pay Cboe Datashop** (~$300–1000) for 7yr 1m VX. Full plan as-written.
4. **Sierra Chart Denali** (user already subscribes; CFE is included on
   Integrated Package 10/11/12; ~$5–15/mo extra). User flagged this as a
   possible cheap path; pending confirmation from Sierra Chart support that
   their existing package includes CFE 1m back to 2019. (Original Sierra
   Chart MES file we received only covered 14:00–20:00 ET — there's a real
   risk their current package is exchange-limited.)

Once Phase 0 is unblocked, the rest of Round 4 follows the parked plan:
Phase A (source admission), Phase B (3 new transforms + 5 new features),
Phase C (2 new strategy classes: `pre_event_drift`,
`cross_asset_divergence_fade`), Phase D (9 pre-registered specs).

A separate "harden-the-system" lane is also scoped: wire holdout into
`auto-test`, replace Bonferroni with Deflated Sharpe Ratio, embed the
five-test triage scorer, add embedding-based hypothesis diversity. Optional;
Round 4 doesn't require it but a clean Round 4 survivor would.

---

## Auto-generation pipeline (verified end-to-end)

`07_auto_generation.md` is the spec. Key points:

- **Phase A**: hypothesis schema + `AutoTestOrchestrator`. Unit-test-driven.
- **Phase B**: Anthropic SDK integration. **`messages.create()`** with
  prose-instructed JSON (not `messages.parse()` — the strict-output grammar
  compiler rejects schemas of our complexity). Cost reporting non-blocking.
- **Phase C**: **feature-stat injection**. Per-feature distribution stats
  (`rows`, `min`, `p10`, `median`, `p90`, `max`) injected into the cached
  registry block of the LLM prompt, with the explicit instruction
  *"thresholds must lie inside [min, max]; tails waste budget"*.

Run order:
```bash
uv run tradegy refresh-feature-stats          # only after re-ingest / new features
uv run tradegy market-scan                    # snapshot current regime → data/market_scan/
uv run tradegy hypothesize --n 3              # LLM brainstorm; reads kill log + latest scan
# inspect; manually flip status: proposed → promoted in YAML
uv run tradegy auto-vary <hyp_id> --n 4       # writes strategies/
uv run tradegy auto-test <hyp_id>             # sanity → walk-forward; JSONL records
```

`hypothesize` automatically pulls (a) the kill log of every previously-
failed hypothesis and (b) the most-recent `market-scan` snapshot, both
injected as system-prompt blocks AFTER the cache breakpoint so neither
invalidates the cached registry prefix. Run `market-scan` before
`hypothesize` if you want the LLM anchored in the current regime.

There is **no `tradegy promote` command** — promotion is a manual edit of
the hypothesis YAML's `status` field. This is intentional: a human gate.

**Cost**: ~$0.07–0.20 per `auto-vary` invocation at default settings. Tracked
post-call, never gated behind `--confirm`.

---

## Discipline (load-bearing — do not relax)

- **Pre-register before backtesting.** Variant budget locked in the hypothesis
  YAML; no post-hoc additions. Three similar lines are better than three
  variants of the same idea — selectivity has been the binding constraint.
- **No simplifications, no fallbacks, no demos, no dummy code.** Address
  root causes. (User's CLAUDE.md repeats this six different ways. Take it
  literally.)
- **No fallback logic.** If something doesn't work it should raise — silent
  degradation hides bugs.
- **Honest kill records.** When a variant fails a gate, record gate, trade
  count, IS/OOS Sharpe, kill reason. Doc 06's status table is where this
  lives.
- **Docs travel with code.** When code changes alter architecture, schemas,
  or contracts, update the relevant `trading_platform_docs/` file in the
  same iteration. (See `~/.claude/projects/-Users-dan-code-tradegy/memory/feedback_docs_with_code.md`.)
- **Commit at end of every round.** Per-round commit with detailed message
  describing changes and reasoning.

---

## Anti-overfitting math currently in use

- **Sanity gate**: ≥30 trades, raw IS Sharpe > 0, no-lookahead audit passes.
- **Walk-forward gate**: avg OOS Sharpe ≥ 50% of avg IS Sharpe AND avg IS
  Sharpe > 0.
- **CPCV gate**: median Sharpe > 0.8 AND pct paths negative < 20%.
- **Holdout gate**: holdout Sharpe ≥ 50% of walk-forward Sharpe.
- **Multiple-comparisons correction**: currently Bonferroni-flavoured
  Sharpe lift `sqrt(2 * ln(N) / T)` across variant pools. **Deflated Sharpe
  Ratio is on the to-do list**; it is the more defensible correction and
  should replace Bonferroni when the auto-gen pipeline is hardened.

---

## Backtest harness defaults (do not change without commit)

- `tick_size = 0.25`
- `slippage_ticks = 0.5/side`
- `commission_round_trip = $1.50`
- Session calendar: `XNYS` for RTH, `globex` for 24h MES.

These are CLI defaults. They match the round-3 conventions and any change
invalidates prior kill records.

---

## Execution layer (doc 11) — shipped

- **FSM**: `PENDING → SUBMITTED → WORKING → (PARTIAL ↔ FILLED) | CANCELLED |
  REJECTED | EXPIRED | UNKNOWN`. Transitions logged.
- **Idempotency**: client order id derived from spec-id + intent — replays
  collapse to the same logical order.
- **Risk caps**: `max_concurrent_instances`, `max_daily_loss_pct`,
  `max_weekly_loss_pct` per spec.
- **Kill-switch**: globally pausable; flush-and-flat on trigger.
- **Session-flatten**: end-of-session forced exit when `end_of_session` is
  declared in the spec.
- **IBKR router**: `place / cancel / query / events`, status mapping into
  the FSM.
- **Divergence detector + reconciliation loop**: detects broker/local state
  drift, alerts, attempts safe re-sync.

Tests live in `tests/test_execution_*.py`.

---

## Live monitoring (doc 12) — Phase 1 shipped

Framework + 4 deterministic checks: `broker_connectivity`, `data_freshness`,
`time_skew`, `process_liveness`. Phase 2+ (data quality, position drift,
PnL anomaly, alert routing) is documented in doc 12 but not yet built.

---

## Data sources currently admitted

`registries/data_sources/`: `mes_1m_ohlcv` (databento, 24h, 7yr,
2019-05-06 → 2026-04-30). The original Sierra Chart export was 14:00–20:00
ET only and is **superseded** by the databento ingest.

Round 4 will add `vix_1m_ohlcv` and `econ_events` once the data path is
unblocked (see "What's next" above).

---

## Memory

User's auto-memory lives at
`~/.claude/projects/-Users-dan-code-tradegy/memory/`. The only entry as of
this pinning is `feedback_docs_with_code.md` (docs travel with code). Add
new memories per the standard auto-memory rules — this CLAUDE.md is for
project state, the memory system is for cross-conversation feedback and
preferences.

---

## Branch state

- `main` (origin): includes PR #7 merge — all auto-gen / execution /
  monitoring work is in main.
- Local feature branch `infra/exec-monitoring-and-autogen-phase-c` is one
  commit behind origin/main (the merge commit). Pull/reset before starting
  new work:
  ```bash
  git checkout main && git pull
  ```
