# Options Volatility Selling Spec

**Status:** Scope (2026-05-02). Adopted as a parallel workstream
after the strategic review highlighted vol-selling on options on
futures as the highest-risk-adjusted reuse of the existing
platform. This doc is the project plan; it will evolve into the
spec for this workstream as Phase A → F unfold.

**Purpose:** Run 5–10 concurrent defined-risk vol-selling positions
on SPX (or /ES) producing 8–15% annualized return with managed
drawdowns and a documented kill chain. Edge source is the
persistent gap between implied and realized volatility, paid by
end-users buying portfolio insurance.

This is **implementation + validation**, not search. tastytrade has
published a decade of research on the framework. The auto-gen
pipeline becomes useful for *parameter sweep within validated
strategy classes*, not for hypothesis ideation.

---

## Implementation status

| Section | Status | Code path |
|---|---|---|
| Project scope (this doc) | ✅ committed 2026-05-02 | this file |
| Phase A — options module skeleton + Greeks | ✅ shipped 2026-05-02 | `src/tradegy/options/` |
| Phase A — ORATS strikes CSV ingest path + chain reader | ✅ shipped 2026-05-03 (decisions in: SPX, ORATS Pro, $25K start) — vendor camelCase columns map to snake_case canonical, parquet date partitions, `iter_chain_snapshots` yields typed `ChainSnapshot` objects, vendor Greeks columns ingested but not propagated to OptionLeg | `src/tradegy/ingest/csv_orats.py`, `src/tradegy/options/chain_io.py`, `registries/data_sources/spx_options_chain.yaml` |
| Phase A — `download_spx_options_orats.py` (ORATS API puller) | ✅ shipped 2026-05-03 | `/Users/dan/code/data/download_spx_options_orats.py` |
| Phase A — chain feature transforms (iv_rank_252d, term_structure_slope, put_call_skew, expected_move_to_expiry, realized_vol_30d) | ⏳ next | (Phase A) |
| Phase A — validation against tastytrade reference backtest | ⏳ next; requires real ORATS data + Phase B harness | (Phase A) |
| Phase B — multi-leg harness extension | ⏳ planned | `src/tradegy/harness/` |
| Phase C — strategy class catalog | ⏳ planned | `src/tradegy/strategies/classes/` |
| Phase D — backtest + walk-forward validation | ⏳ planned | (Phase D) |
| Phase E — paper trading via IBKR combo orders | ⏳ planned | `src/tradegy/execution/` |
| Phase F — 90-day live soak | ⏳ planned | (Phase F) |

---

## Goal + non-goals

**Goal:** Systematic vol-selling on SPX or /ES options, running
5–10 concurrent defined-risk positions producing 8–15% annualized
return with documented drawdown profile and pre-registered kill
criteria.

**Non-goals:**

- Naked / undefined-risk positions (XIV reasons).
- Single-leg directional speculation.
- Sub-day timeframes (vol-selling is a 30–45 DTE business).
- LLM-driven strategy *invention* — this is implementation, not
  search. Auto-gen helps with parameter sweeps within validated
  frameworks, not with hypothesis generation.
- 0DTE / 1DTE strategies (different game, deferred).
- Equity options on individual names (different liquidity, earnings
  risk; deferred).

---

## Scope

**In scope:**

- Iron condor (delta-anchored entry, DTE-anchored management).
- Put credit spread (directional, defined-risk).
- Short strangle WITH wings (defined-risk via long protective wings).
- Calendar spread (term-structure capture).

**Out of scope:**

- Naked strangles, naked puts, naked calls.
- Ratio spreads without protective wings.
- Single-stock options.

---

## Phase plan

### Phase A — data + greeks foundation

**Goal:** Reproduce a published tastytrade backtest result (SPX 1SD
short strangle, 45 DTE, manage at 21 DTE) within ±10% on our
harness. Without that, downstream phases are building on sand.

**Decisions needed before Phase A starts:**

1. **Underlying:** SPX (recommended — cleanest historical data,
   European-style, no early-assignment risk) vs /ES (futures
   options, slightly different settlement). Recommend starting on
   SPX, porting to /ES if margin efficiency demands it.
2. **Data vendor:** ORATS Pro (~$199/mo, 5+ years daily SPX
   chains with computed Greeks) vs CBOE DataShop (~$1000+ one-time
   bulk historical) vs databento OPRA (enterprise pricing).
   Recommend ORATS subscription for development, evaluate buyout
   later.

**Build:**

- `src/tradegy/options/` module:
  - `chain.py` — ChainSnapshot + OptionLeg dataclasses
  - `greeks.py` — Black-Scholes Greeks (vendor-independent
    computation we control)
  - `iv_surface.py` — strike + expiry interpolation utilities
- New data source registry kind: `options_chain` (cadence: daily;
  schema: ts_utc, underlying_price, strike, expiry, call/put bid/
  ask/iv/volume/oi)
- New feature category: chain-snapshot features (separate cadence
  from bar-stream features, same no-lookahead discipline)
- New chain features: `iv_rank_252d`, `iv_percentile_252d`,
  `term_structure_slope_30_60`, `put_call_skew`,
  `expected_move_to_expiry`, `realized_vol_30d`

**Validation requirement:** Reproduce a published tastytrade
result within ±10%. Their research blog has multiple specific
backtests with full parameters disclosed. If our number doesn't
match, our harness is wrong and we don't proceed.

**Estimate:** 4–6 weeks.

### Phase B — multi-leg harness extension

The current harness is single-leg, bar-driven. Vol-selling is
multi-leg, bar-driven (daily check-ins suffice — nobody is queue-
arbing iron condors).

**Build:**

- Multi-leg position model — track each leg independently,
  aggregate to portfolio Greeks.
- Daily theta accrual — every bar advances time-to-expiry; P&L
  marks against current chain snapshot.
- Multi-leg margin model — defined-risk = max-loss = capital
  reservation.
- Early-management triggers: close at 50% max profit, close at
  21 DTE regardless of P&L, close at 200% loss (configurable).
- Multi-leg fill simulation — mid-price ±N cents with realistic
  option-spread modeling.
- Position-level + portfolio-level Greeks monitoring.

**Reuse estimate:** ~60% of existing harness reuses (time-stepping,
session boundaries, evidence packets, statistics). ~40% extends
(position model, fill simulator, cost model).

**Estimate:** 3–4 weeks.

### Phase C — strategy class catalog

Pre-register a fixed set of variants and benchmark each against
published research:

- `iron_condor_45dte_d16` — 16-delta condor, 45 DTE, close at 50%
  / 21 DTE.
- `iron_condor_45dte_d10` — wider (less premium, fewer tests).
- `put_credit_spread_45dte_d20` — directional, capture put skew.
- `short_strangle_defined_45dte_d16` — narrow body + wider wings
  (different P&L profile than iron condor).
- `calendar_put_30_60` — short 30-DTE put, long 60-DTE put,
  same strike.

Each variant: full pre-registration per the anti-overfitting
discipline established in `06_hypothesis_system.md` and
`07_auto_generation.md`.

**Estimate:** 2–3 weeks.

### Phase D — backtest + walk-forward

- Run all 5 variants through harness on 5+ years of SPX history.
- Compare against tastytrade-published results (mandatory: ±10%
  reconciliation).
- Walk-forward validation. **CPCV does not apply cleanly** because
  vol-selling positions are highly autocorrelated (~30-day
  holding period; today's "trade" overlaps yesterday's). Use
  block-bootstrap with block size = average holding period
  instead.
- Risk metrics that matter for vol selling: **max drawdown,
  time-to-recovery, worst-month, tail ratio, Sortino**. Sharpe
  alone is misleading (fat-left-tailed return distribution).

**Estimate:** 2–3 weeks.

### Phase E — paper trading (IBKR combo orders)

- Wire IBKR multi-leg combo orders into the execution layer
  (IBKR supports natively; needs adapter extension).
- Mid-price entry with timeout escape to slightly worse fill.
- Run paper account with 1 contract per position.
- Weekly comparison: paper P&L vs backtest P&L for the same
  period (must track within ±15%).
- Identify and fix divergence (commission model, fill
  assumptions, Greeks staleness).

**Estimate:** 4–8 weeks bedding-in.

### Phase F — live, scaled (90-day soak)

- Start at **25% of intended capital allocation**.
- Hard rule: scale up only if first 90 days produces no surprise
  (drawdown within model, fill quality matches paper).
- Full allocation only after 90 days of clean operation.

---

## What changes in the codebase

**Reuses cleanly (~60%):**

- Time-stepping loop, session boundaries.
- No-lookahead validation framework.
- Walk-forward harness (`harness/walk_forward.py`).
- Evidence packets + signing infrastructure.
- FSM execution lifecycle (`execution/lifecycle.py`).
- Idempotency, kill-switch, monitoring framework.
- Anti-overfitting discipline (pre-registration, evidence packets).
- IBKR connection (extends for combo orders, doesn't replace).

**Material extension:**

- `harness/execution.py` — multi-leg fill simulation, options-aware
  cost model.
- New `src/tradegy/options/` module — chain ingest, Greeks
  computation, IV surface utilities.
- Strategy ABC — extend to multi-leg position management
  (currently `Order → Position`; needs `OrderGroup →
  MultiLegPosition`).
- New feature category — chain-snapshots alongside bar-streams.
- Risk module — portfolio Greeks monitoring, concentration limits.

**No throwaway.** The bar-driven signal-edge work continues to
function; vol-selling is a parallel strategy class category in
the same platform.

---

## Cost + capital + time

| Item | Cost |
|---|---|
| ORATS Pro (historical chains + Greeks) | $199/mo dev; can drop after backtest is validated |
| IBKR pro tier (live options data) | ~$10–20/mo |
| Compute / storage | negligible (chain data is small relative to L2) |
| **Recurring** | **~$200/mo dev; ~$20/mo steady state** |

| Capital tier | What it buys |
|---|---|
| $10–25K | 2–3 small concurrent condors, margin tight, slow capital turnover |
| $25–50K | 5–10 concurrent positions, proper diversification, normal operation |
| $110K+ (IBKR Portfolio Margin threshold) | 4–5x capital efficiency, professional-scale operation |

**Time-to-first-paper-trade:** ~3 months solo dev.
**Time-to-live:** ~5 months including paper bedding-in.
**Scale-to-full:** 8–9 months from start.

---

## Risk catalog (load-bearing — non-optional)

Every one of these has killed retail vol sellers. None are
optional. Each must be implemented as code, not as discipline:

1. **Defined-risk only.** Every position has long protective wings.
   No naked anything. Capital at risk = max loss = known at entry.
2. **Tail-event protocol.** When VIX spikes >50% in a day,
   automatic position-size reduction. When realized vol exceeds
   95th percentile of trailing year, halt new entries.
3. **Concentration limit.** No more than 25% of capital in any
   single expiration cycle.
4. **Early-management discipline.** Close at 21 DTE regardless of
   P&L. Pin risk near expiration kills accounts.
5. **Margin buffer.** Maintain 40% free buying power minimum.
   Defined-risk positions can still see margin escalate 5–10x
   during stress.
6. **Liquidity-aware exits.** During stress, wide spreads can mean
   exit slippage > premium collected. Pre-defined "liquidity
   emergency" protocol: close at market, accept slippage,
   preserve capital.
7. **Sequence-of-returns honesty.** Backtest reports must include
   the worst 5% of months prominently, not buried. Vol-selling
   P&L is left-fat-tailed; the average masks the tail.
8. **Underlying selection.** SPX/ES only initially. Single-stock
   options carry earnings + gap risk the model doesn't price.

---

## Decision points before Phase A starts

1. **SPX vs /ES** — recommend SPX (cleaner data, European-style,
   no early assignment).
2. **ORATS vs CBOE DataShop vs free** — recommend ORATS
   subscription for development.
3. **Capital target for live phase** — $50K is the minimum where
   the math works; $25K is workable for paper + tiny live.
4. **Account type at IBKR** — standard cash vs portfolio margin
   ($110K minimum). PM dramatically improves capital efficiency
   for spreads but raises the floor.

---

## Comparison to current state

Three killed regime-anchored fade hypotheses (cumulative 15
killed across the project), ~10K trades, average -0.4 Sharpe.

The vol-selling path:

- Edge is **structural and documented**, not searched-for.
- Distribution of returns is fat-left-tailed but mean is positive
  (with discipline).
- Reuses 60% of platform; ~3 months to first paper trade.
- Capital floor higher ($25–50K vs MES intraday's effectively
  zero).
- Realistic: 8–15% annualized with drawdowns occasionally hitting
  20–25%.

Different game than what we've been playing. Rewards the
validation infrastructure we built. Punishes carelessness about
tail risk. Accessible to a disciplined retail trader with $25K+
today.
