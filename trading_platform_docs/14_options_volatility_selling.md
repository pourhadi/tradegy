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
| Phase A — end-to-end verification on real ORATS data | ✅ verified 2026-05-03 (5 trade days, 5 snapshots, ~22.5K legs each, 54 expiries; chain reader yields typed objects; recomputed Greeks agree with vendor delta within 0.27%; vendor unit conventions documented) | (Phase A) |
| Phase A — chain feature transforms (atm_iv, term_structure_slope, put_call_skew_25d, expected_move_to_expiry, iv_rank_252d, iv_percentile_252d, realized_vol_30d) | ✅ shipped 2026-05-03 — pure functions over ChainSnapshot(s); 13 unit tests against synthetic chains; smoke-tested against real ORATS data (5 SPX days Dec 15-19 2025: ATM IV 12-14%, 25-delta put skew +0.04-0.05 matching practitioner expectations, term structure -0.005 to -0.016 contango as expected, IV regime jumped on Dec 17 when SPX dropped 1%) | `src/tradegy/options/chain_features.py` |
| Phase A — validation against tastytrade reference backtest | ⏳ next; requires Phase B multi-leg harness + a longer ORATS pull | (Phase A) |
| Phase B-1 — position + cost model | ✅ shipped 2026-05-03 — OptionPosition (signed quantity convention), MultiLegPosition (4-leg iron condor verified on real SPX data: $1,676 credit, $8,324 max loss, 20.1% credit/risk), OptionCostModel (mid ± offset_fraction*half_spread fills + per-leg commission), compute_max_loss_per_contract (closed-form match for iron condor + put credit spread, generic payoff-curve sampler for arbitrary multi-leg shapes) | `src/tradegy/options/positions.py`, `src/tradegy/options/cost_model.py` |
| Phase B-2 — strategy ABC + multi-leg backtest runner | ✅ shipped 2026-05-03 — `OptionStrategy` ABC, `ManagementRules` + `should_close` (50%/21 DTE/200% loss applied uniformly by the runner, never by strategies), `IronCondor45dteD16` strategy class with delta-anchored short body + delta-anchored long wings (fixes the asymmetric-wing issue surfaced in B-1), `run_options_backtest` runner with mark-to-market + management + fill-at-next-snap (no same-bar lookahead) + commission accounting + per-snap P&L trajectory + per-trade close records. End-to-end real-data smoke test: entered 45-DTE 16-delta condor on 2025-12-15 SPX, position up $764 by Dec 19 from vol decay, no triggers fired (all far from 21 DTE) | `src/tradegy/options/strategy.py`, `src/tradegy/options/runner.py`, `src/tradegy/options/strategies/iron_condor.py` |
| Phase B-3 — portfolio Greeks + risk caps | ✅ shipped 2026-05-03 — `RiskConfig` (declared_capital, max_capital_at_risk_pct, max_per_expiration_pct, suspend_above_rv_pct), `RiskManager.evaluate_order` returns approve/reject with audit reason, `PortfolioGreeks` + `compute_portfolio_greeks` for per-snapshot aggregate dollar exposure (delta/gamma/theta/vega in trader units), runner integration with `RejectedOrder` audit trail. Real-data verified: $25K cap correctly rejects the $48K condor from B-2 with 4 capital_cap rejections; $250K cap lets it through unchanged; portfolio Greeks at end-of-window match vol-selling expectations (theta +$104/day positive, vega -$690/vol-pt negative, gamma -$51K short-gamma signature). | `src/tradegy/options/risk.py`, updated `runner.py` |
| **Phase B complete** | ✅ 2026-05-03 — multi-leg position model + cost model + strategy ABC + management rules + iron condor + backtest runner + portfolio Greeks + risk caps all shipped + verified end-to-end on real ORATS SPX data | (Phase B) |
| Phase C-1 — PutCreditSpread (2-leg directional defined-risk) | ✅ shipped 2026-05-03 — delta-anchored entry (default short -0.30, wing -0.05), inherits all management/risk infrastructure. Real-data smoke surfaced a known credit-spread design issue: 5-delta wings on SPX put-skew yield $775-wide spreads with only 9% credit/risk vs the typical 20-30% practitioner range. Strategy is mechanically correct; a fixed-dollar-width variant is the practical follow-up. | `src/tradegy/options/strategies/put_credit_spread.py` |
| Phase C-2 — ShortStrangleDefined (4-leg, narrow 25-delta body) | ✅ shipped 2026-05-03 — same shape as iron condor with NARROWER body. Real-data side-by-side on 2025-12-15 SPX confirms the trade-off: body sits $219/$231 from spot (vs condor's $394/$281), credit $7,810 (77% more than condor's $4,414), credit/risk 12.6% (vs 9.2%). Includes refactor: `_helpers.py` extracted shared leg-selection primitives (pick_expiry_closest_to_dte, is_fillable, closest_delta) used by all three strategy classes. | `src/tradegy/options/strategies/short_strangle_defined.py` |
| Phase C-3 — PutCalendarSpread (debit, two expiries, ATM) | ✅ shipped 2026-05-03 — short front-month put + long back-month put at same ATM strike. Net debit position. Required infrastructure extensions: `MultiLegPosition.pnl_pct_of_debit` + `ManagementRules.profit_take_pct_of_debit/loss_stop_pct_of_debit` + `should_close` dispatches credit-vs-debit branch automatically. Real-data verified on 2025-12-15 SPX: 6820 strike (ATM, $0.31 from spot), 29/66 DTE pair, $5,426 debit, max_loss=debit (correct closed-form for same-strike calendars), day-5 P&L +1.0% on debit. | `src/tradegy/options/strategies/put_calendar.py`, updated `positions.py`, `strategy.py` |
| **Phase C complete** | ✅ 2026-05-03 — four strategy classes shipped (iron_condor + put_credit_spread + short_strangle_defined + put_calendar), all with delta-anchored leg selection, all sharing the same management discipline + risk gates, all verified end-to-end on real ORATS SPX data | (Phase C) |
| Phase D-0 — full-year ORATS SPX pull | ✅ 2026-05-03 — 250 trade days (2025-01-02 → 2025-12-31), 1.5 GB CSV, 2.7M unique rows after dedup ingested into 250 date partitions | (data) |
| Phase D-1 — close-P&L sign bug fix | ✅ 2026-05-03 — first full-year backtest produced impossible 100% hit rate / $163K P&L / $0 drawdown. Inspection: trade 5 fired loss_stop at -239% of credit but recorded +$22.6K P&L. Root cause: `_close_position` had `closed_credit = -close_per_share` then `pnl = entry_credit - closed_credit` which produced `pnl = entry_credit + close_cost` (opposite sign of actual realized P&L). Fixed by computing `pnl = entry_credit - close_cost` directly (matches mark_to_market formula). Added regression tests `test_options_pnl_invariants.py`: close P&L MUST agree in sign with mark_to_market; full-year backtest hit rate < 100% AND max drawdown < 0 (the bug signature). | `src/tradegy/options/runner.py`, `tests/test_options_pnl_invariants.py` |
| Phase D-2 — first real backtest of all 4 strategies | ✅ 2026-05-03 (post-fix) — realistic numbers within practitioner-expected ranges. Iron Condor 16d: 67% hit / -0.3% RoC. PutCreditSpread 30d: 85% hit / +7.7% RoC (benefited from 2025 SPX bull trend). ShortStrangleDefined 25d: 53% hit / -9.7% RoC (narrow-body underperformed). PutCalendar 30/60 ATM: 75% hit / -0.4% RoC with tightly-controlled drawdown ($4.8K vs $32K for credit spreads). All exhibit fat-left-tailed return distributions characteristic of vol selling. | (Phase D) |
| Phase C-extended — three new strategy classes (CCS, IBF, JL) + width-variant parameterization (PCS, CCS, IC, Strangle) | ✅ shipped 2026-05-03. CallCreditSpread (bearish mirror of PCS), IronButterfly (concentrated condor at ATM), JadeLizard (asymmetric defined-risk targeting no-upside-loss when credit ≥ call wing width). Plus optional `wing_width_dollars` on the four credit-spread classes — when set, replaces delta-anchored wings with fixed-dollar-width selection (addresses the C-1 finding that 5-delta SPX wings produce $400-700 wide spreads with poor c/r). 7 → 7+4 = 11 effective strategy variants. | `src/tradegy/options/strategies/{call_credit_spread,iron_butterfly,jade_lizard}.py` + parameterized existing classes |
| Phase D-3 — multi-strategy real-data backtest | ✅ run 2026-05-03 on full 2025 SPX. RESULT TABLE below documents the relative outcomes — **only PutCreditSpread (delta-anchored) was profitable** (+7.7% RoC, benefited from SPX bull trend). All other strategies ranged from break-even (IC, CCS, calendar) to lossy (Strangle -9.7%, Butterfly -9.9%). Width-anchored variants had MUCH smaller drawdowns ($5K vs $22-50K) but also smaller per-trade credits → underperformed delta-anchored in 2025's benign regime. The trade-off would invert in stress years (2018, 2020, 2022). Multi-year walk-forward needed to validate. | `tests/test_options_*.py` + scripted comparisons |
| Phase D-4 — multi-year backtest (6 years, 2020-2025) | ✅ shipped 2026-05-03 — 1508 trade-day partitions, 7.5GB CSV ingested. All 12 strategy + IV-gating variants backtested on the full window. Per-year breakdown of top-3 (PCS, JL, IC) shows real regime sensitivity. | (Phase D) |
| Phase D-5 — management-rule sweep (Tier 1) | ✅ shipped 2026-05-03 — `scripts/management_sweep.py` runs [PCS, IC, JL] × 6 management-rule variants on 6-year SPX. **Headline finding: JadeLizard + tight 25/21/200 management beats PCS bare on every metric** — annualized RoC +4.3% vs +3.0%, Sharpe 0.49 vs 0.34, max DD -$29K vs -$49K, 5/6 positive years (only -8.3% in 2022 bear vs PCS bare -15.4%). Default mgmt is sub-optimal for JL (which collects high credit per trade — 25% target captures the win faster). PCS prefers MORE time (50/14 → +3.5% vs default +3.0%, sharpe 0.40). IC dramatically WORSE under tight 25%/21 (-2.7% vs +1.0%) — IC needs more time to capture credit. | `scripts/management_sweep.py` |
| Phase D-6 — Tier 2 strategy expansion | ✅ shipped 2026-05-03 — `PutBrokenWingButterfly45dte` (3-leg asymmetric: 2 short body + 1 long inner wing + 1 long outer wing; default 25/75 dollar wings produces small credit + capped downside + uncapped-upside-with-no-loss above K1) and `PutDiagonal30_60` (different from calendar — DIFFERENT strikes; bullish bias). Multi-year results (2020-2025): PBWB +0.2% AnnRoC default / -0.1% with tight management — barely profitable but with the smallest max DD of any strategy ($-7K). PutDiagonal +2.0% AnnRoC, Sharpe 0.32, 299 trades (2.3x more frequent entry than other strategies — front-month decay fires often). Neither beat JL+tight 25/21 (+4.3% AnnRoC, Sharpe 0.49) which remains the validated multi-year winner. | `src/tradegy/options/strategies/{put_broken_wing_butterfly,put_diagonal}.py` |
| Phase D-7 — Tier 3 regime-conditioned wrappers + structural variants | ✅ shipped 2026-05-03 — `SkewGatedStrategy` (rolling rank of put_call_skew_25d, gates on min/max), `TermStructureGatedStrategy` (live near-vs-far ATM IV slope, gates on contango/backwardation), `ReverseIronCondor45dteD30` (long-vol structure, debit, hedge for short-vol majority), `CallDiagonal30_60` (bearish mirror of PutDiagonal). Multi-year results: NONE beat JL+tight 25/21 (+4.3% AnnRoC, Sharpe 0.49). Best risk-adjusted: **JL+tight + term-structure max_slope=0 → +3.8% AnnRoC, Sharpe 0.53** (slight Sharpe improvement at cost of absolute return). Skew-gating underperformed counter to practitioner thesis. RIC barely break-even (long-vol in bull window). CallDiagonal lost -4% (6-year bull market). | `src/tradegy/options/strategies/{skew_gated,term_structure_gated,reverse_iron_condor,call_diagonal}.py` |
| Phase D-8 — formal walk-forward + multi-instrument | ⏳ next | (Phase D) |
| IV-gated strategy wrapper | ✅ shipped 2026-05-03 — `IvGatedStrategy(base, min_iv_rank, max_iv_rank, target_dte, window_days, min_history_days)` composes any OptionStrategy with an entry gate based on rolling ATM-IV rank. | `src/tradegy/options/strategies/iv_gated.py` |
| 2025-only IV-gating findings | ❌ NOT validated by multi-year. The 2025-only "PCS+IV<0.30 → +10.2% RoC, 90% hit" was regime-local; on 6 years it drops to +7.4% with $49K max DD. The "IC width $50 + IV>0.50 → 5/5 perfect" was sample-noise; on 6 years it's -2.7% RoC, 43% hit. Lesson: don't infer from one year. | (validated against multi-year) |

### Multi-year (2020-2025) backtest results — single-strategy, $250K, default management

| Strategy | Trades | Hit% | P&L | MaxDD | Sharpe | RoC |
|---|---|---|---|---|---|---|
| **PutCreditSpread 30d (delta wing)** | **132** | **81%** | **+$45,038** | -$48,716 | **+0.34** | **+18.0%** ← winner |
| JadeLizard 45dte | 95 | 63% | +$23,652 | -$55,945 | +0.17 | +9.5% |
| IronCondor 16d (delta wing) | 104 | 60% | +$14,760 | -$24,645 | +0.19 | +5.9% |
| IronCondor 16d (width $50) | 94 | 46% | -$16,011 | -$17,117 | -0.75 | -6.4% |
| PCS + IV>0.50 (canonical practitioner rule) | 50 | 70% | -$8,094 | -$32,212 | -0.09 | -3.2% |
| PCS + IV<0.30 (the 2025 "winner") | 95 | 82% | +$18,444 | -$48,745 | +0.21 | +7.4% |
| PutCalendar 30/60 ATM | 214 | 60% | -$6,700 | -$10,436 | -0.31 | -2.7% |
| IC + IV>0.50 (60d) | 43 | 56% | -$30,827 | -$51,462 | -0.42 | -12.3% |
| IC width $50 + IV>0.50 (the "perfect 5/5") | 42 | 43% | -$6,777 | -$9,089 | -0.49 | -2.7% |
| ShortStrangle 25d | 94 | 50% | -$32,145 | -$71,879 | -0.27 | -12.9% |
| CallCreditSpread 30d (bull years dominated) | 125 | 53% | -$59,524 | -$71,658 | -0.53 | -23.8% |
| IronButterfly ATM | 88 | 60% | -$73,365 | -$102,348 | -0.42 | -29.3% |

### Per-year breakdown (top 3)

| | 2020 (COVID) | 2021 (bull) | 2022 (bear) | 2023 (recovery) | 2024 (bull) | 2025 (bull) | Pos years |
|---|---|---|---|---|---|---|---|
| **PCS bare** | +2.8% | +11.8% | **-15.4%** | +5.7% | +10.7% | +2.4% | **5/6** |
| JadeLizard | -0.9% | +9.8% | +6.0% | +5.6% | -1.5% | -9.6% | 3/6 |
| IronCondor 16d | -3.6% | +2.7% | **+7.7%** | -5.5% | +3.0% | +1.6% | 4/6 |

PCS is consistent except in bear years (2022 took ~-15%). IronCondor was POSITIVE in 2022 when PCS lost — natural regime hedge. JadeLizard inconsistent.

### Real findings (validated across 6 years)

1. **PutCreditSpread bare is the only consistently profitable single-strategy** at ~3% annualized RoC, 5/6 positive years, Sharpe 0.34.
2. **Width-anchored variants all lost** across 6 years — small per-trade credit didn't compound past the rare max-loss events. Their drawdown advantage doesn't compensate for the credit reduction. Useful for stress-only deployment perhaps.
3. **IV-gating UNDERPERFORMED bare strategies** across 6 years. Both directions (IV>0.50 and IV<0.30) hurt PCS relative to the bare strategy. The canonical "sell vol when IV is high" rule didn't help on SPX 2020-2025.
4. **2025-only findings did not generalize.** The +10.2% / 90% hit / $3K DD result for PCS+IV<0.30 was regime-local; the "perfect 5/5" for IC+width+IV-gate was sample-noise.
5. **IC is the natural regime hedge for PCS.** When PCS loses (2022 bear), IC was its best year. A combined portfolio is the obvious next experiment but requires careful capital-allocation design (single-position concentration limits make naive 50/50 allocation underperform single-strategy with full capital).

### 2025 backtest result table (250 trade days, $250K capital, default management)

| Strategy | Trades | Hit% | P&L | Max DD | RoC |
|---|---|---|---|---|---|
| **PutCreditSpread 30d (delta wing)** | **20** | **85%** | **+$19,241** | -$32,718 | **+7.7%** |
| IronCondor 16d (delta wing) | 18 | 67% | -$626 | -$22,500 | -0.3% |
| PutCalendar 30/60 ATM | 36 | 75% | -$904 | -$4,799 | -0.4% |
| CallCreditSpread 30d (delta wing) | 21 | 71% | -$1,822 | -$25,622 | -0.7% |
| PutCreditSpread 30d (width $50) | 19 | 74% | -$2,343 | -$8,022 | -0.9% |
| PutCreditSpread 30d (width $25) | 18 | 72% | -$3,249 | -$6,264 | -1.3% |
| IronCondor 16d (width $50) | 15 | 60% | -$3,929 | -$5,036 | -1.6% |
| CallCreditSpread 30d (width $25) | 18 | 56% | -$5,479 | -$7,079 | -2.2% |
| ShortStrangle 25d (width $50) | 14 | 50% | -$6,286 | -$7,350 | -2.5% |
| IronCondor 16d (width $100) | 15 | 53% | -$8,579 | -$10,656 | -3.4% |
| JadeLizard 45dte | 16 | 81% | -$10,617 | -$50,948 | -4.2% |
| ShortStrangle 25d (delta wing) | 15 | 53% | -$24,348 | -$39,270 | -9.7% |
| IronButterfly ATM (delta wing) | 14 | 43% | -$24,689 | -$35,935 | -9.9% |
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

**Status: groundwork shipped 2026-05-03; full integration pending operator paper account setup.**

- ✅ Wire IBKR multi-leg combo orders into the execution layer
  (`src/tradegy/execution/ibkr_options_router.py`):
  - `IbkrOptionsRouter`: option contract resolution per leg
    (qualifies via IBKR's `qualifyContracts`), BAG combo
    construction with proper ratios + actions, single
    LimitOrder with net price (positive for both credit + debit
    — sign comes from BUY/SELL action). One ManagedOrder per
    combo even with N legs (preserves defined-risk invariant).
  - Lifecycle integration: combo placement → SUBMITTED transition
    via the existing FSM. Status / fill events translate via
    `map_ibkr_status` (shared with the futures router).
  - Idempotency at `client_order_id` level. Cancel + get_combo
    + health surface. Subscriber notification.
  - 10 tests against a MockIB; full suite 572 passing.
- ⏳ Mid-price entry with timeout escape to slightly worse fill —
  current implementation places at cost-model-computed mid; the
  escalation policy (mid → mid + offset → mid + 2*offset → ask)
  needs the live event loop and is straightforward to add once
  the runner integration lands.
- ⏳ Runner integration: `_open_position_from_order` currently
  uses the cost model for backtest fills. Paper/live mode needs
  a code path that calls `router.place_combo()` and awaits the
  FILLED transition. Async + timeout handling; ~1-2 days of work.
- ⏳ Run paper account with 1 contract per position. Operator-
  side: install ib_async, run TWS/Gateway in paper mode, point
  the router at the paper account.
- ⏳ Weekly comparison: paper P&L vs backtest P&L for the same
  period (must track within ±15%).
- ⏳ Identify and fix divergence (commission model, fill
  assumptions, Greeks staleness).

**Estimate (remaining):** 4–8 weeks bedding-in once operator
paper-account setup is complete.

**Operator workflow (for when ready):**
  1. Open IBKR paper account (free, ~15 min).
  2. Install Trader Workstation (TWS) or IB Gateway in paper mode.
  3. `pip install ib_async`.
  4. Configure TWS API: enable client connections on port 7497
     (paper) / 4002 (gateway paper).
  5. Build an `IBKRConnection` instance (`src/tradegy/live/ibkr_connection.py`).
  6. Construct `IbkrOptionsRouter(ib=conn.ib, cost_model=...)`.
  7. Wire to runner — Phase E full integration code path.

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
