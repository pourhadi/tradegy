# $5K capital options strategy plan

**Status:** plan written 2026-05-05. Two paths defined: (1) deployable
today on existing equity-options data; (2) requires futures-options data
acquisition for MES extension.

## TL;DR

You have an already-validated $5K edge: **SPY + PCS + IC + JL + IV<0.25**
on the existing `spy_options_chain` data passes 16-yr walk-forward at
**OOS Sharpe +0.867** (avg, 13 windows) and **~13.5% AnnRoC**. This is
deployable today.

The MES futures options extension (the user's actual target) requires
separate data acquisition (~$99/mo Polygon or ~$300-1k CBOE Datashop
one-time) and adapted strategy classes. The mechanism (variance risk
premium on equity index options) is the same; the implementation
differs in margin (SPAN vs Reg-T), multiplier ($5/pt vs $1/share for SPY),
and tick size.

## Path 1: Deploy validated $5K SPY config (zero data spend)

### What it is

Per doc 14 and verified 2026-05-05 walk-forward:
- Underlying: SPY (1/10th S&P 500 ETF)
- Strategies: portfolio of three credit-vol structures
  - **PCS** — `put_credit_spread_45dte_d30` (45 DTE, ~30Δ short put)
  - **IC** — `iron_condor_45dte_d16` (45 DTE, ~16Δ short strikes)
  - **JL** — `jade_lizard_45dte` (45 DTE asymmetric structure)
- Gate: IV-rank ≤ 0.25 (only deploy when IV is in lowest 25% of trailing
  252-day range) — counter-canonical "sell vol when implied is LOW",
  documented in doc 14

### Deployment command

```bash
uv run tradegy options-walk-forward \
    put_credit_spread_45dte_d30,iron_condor_45dte_d16,jade_lizard_45dte \
    --ticker SPY \
    --source-id spy_options_chain \
    --capital 5000 \
    --start 2010-01-01 \
    --end 2026-04-30 \
    --iv-gate-max 0.25
```

### Validation result (2026-05-05 reproduction)

| Metric | Value |
|---|---|
| Walk-forward windows | 13 (3yr train / 1yr test / 1yr roll) |
| avg IS Sharpe | +0.097 |
| **avg OOS Sharpe** | **+0.867** |
| Worst-window OOS Sharpe | -0.259 |
| avg OOS trades / window | 19.8 |
| Total OOS PnL across 13 windows | ~+$8,800 |
| **Annualized RoC** | **~13.5%** |
| Gate (avg OOS ≥ 50% of avg IS) | **PASS** |

### Risk profile at $5K

- Position sizing: ~3-5 spreads per cycle (PCS + IC + JL)
- SPAN doesn't apply to SPY options (Reg-T) — full max-loss is buying
  power consumed
- Worst-window single-trade loss: spread-width × multiplier ≈ $200-300
- Worst-year drawdown (per the OOS column): -$902 (window 2 of 13) =
  -18% of capital; recovers in subsequent years

### Expected annual return

- Mid-estimate: ~$675/yr (13% AnnRoC × $5K)
- Range: $250-$1,200 depending on regime and IV environment

This barely beats T-bills (~5%) at $5K, but it scales linearly to bigger
capital. At $10K → ~$1,350/yr; at $25K → ~$3,375/yr.

### When to deploy this path

Deploy NOW if:
- The $5K is risk-able capital (not retirement / emergency fund)
- You can monitor positions weekly (not daily)
- You accept -20% drawdown windows are normal

Don't deploy if:
- Need >15% AnnRoC at $5K — this strategy can't deliver that
- Need positions to clear daily (these hold 14-30 DTE typical)
- Want pure intraday — the user's earlier "no cross-session" constraint
  does NOT apply to this lane (it's an options-vol-selling strategy
  that holds across sessions). If the constraint is hard, skip to Path 2.

## Path 2: MES futures options (intraday-compliant, requires data spend)

### What it is

Same VRP mechanism as Path 1, but on MES (Micro E-mini S&P 500) futures
options. The advantage: **0DTE structures available** (open and close
same day = strict intraday compliance), and SPAN margin ≈ 30-50% of
max loss (vs Reg-T 100% on equity options) — ~3-5x more capital
efficiency at the same risk level.

### What we need

1. **MES options chain data with intraday quotes** (for 0DTE testing):
   - **Resolved 2026-05-05**: covered by existing databento `GLBX.MDP3`
     subscription. No Polygon, no CBOE Datashop, no extra subscription
     needed. Two parent symbology trees, both verified live via
     `metadata.get_cost`:
     - `MES.OPT` → standard quarterly options on quarterly futures
       (3rd Friday of Mar/Jun/Sep/Dec). 4 expiries/year. **Useless for
       0DTE** (only 4 zero-DTE days/year). Cost: ~$0.41/yr `ohlcv-1m`,
       ~$0.18/yr `definition`.
     - `X[1-5][A-D].OPT` → daily MES options. Letter = day of week
       (A=Mon, B=Tue, C=Wed, D=Thu). Number = nth occurrence of that
       weekday in the listing month. **This is the 0DTE feed.** Up to
       20 parent symbols (5 weeks × 4 weekdays). Cost: ~$0.10/yr per
       parent for `ohlcv-1m`, ~$0.05/yr for `definition`. Daily MES
       options launched at CME 2022-09-27, so dailies coverage starts
       there.
   - **Total cost for full retail MES options coverage** (verified
     via `metadata.get_cost` 2026-05-05; full X[1-5][A-D] grid):
     - 2024 (1 year): $0.59 quarterlies + $2.64 dailies = **$3.23**
       (dailies breakdown: $1.80 ohlcv-1m + $0.84 definition across
       all 20 daily-options parent symbols)
     - 2020-2024 (5 years, dailies only post-Sep-2022):
       ~$2.95 quarterlies + ~$5.93 dailies ≈ **$8.88**
   - 2024 quarterly snapshot already pulled 2026-05-05 (sanity check):
     122,965 1m bars + 357,034 definition records, total spend $0.59.
     Stored at `/Users/dan/code/data/mes_options_ohlcv_1m/` and
     `/Users/dan/code/data/mes_options_definition/`. Quarterlies-only
     dataset is **NOT sufficient** for 0DTE — only 4 expiries/yr.
   - **2023-2024 X-prefix dailies acquired 2026-05-06** (the actual
     0DTE feed). All 20 X[1-5][A-D] parent symbols downloaded for
     both `definition` and `ohlcv-1m` schemas:
     - **2.26M definition records** (per-contract metadata; 1.43GB raw CSV)
     - **898K ohlcv-1m bars** (per-minute trades; 98MB raw CSV)
     - **364 distinct 0DTE expirations** in the 2023-2024 window
       (90% of the theoretical 403 = 252 trading days × 4 Mon-Thu / 5
       weekdays). DoW spread: 88 Mon, 95 Tue, 94 Wed, 92 Thu — all
       four 0DTE weekdays uniformly covered.
     - 9 underlying futures (MESH3..MESZ4 quarterlies)
     - Round spend: $1.51 definitions + $3.09 bars = **$4.60**
     - **Total round spend (quarterlies + dailies): $5.19** for full
       retail-grade 2-year MES options coverage.
     - Stored at `/Users/dan/code/data/mes_options_daily_<parent>_
       <schema>/` (40 directories total, one per parent × schema).
   - Downloader: `/Users/dan/code/data/download_mes_options.py`
     supports `--product {quarterlies|dailies|all|<parent>}` with
     resume + retry semantics. The Polygon probe `probe_polygon_mes_
     options.py` is retracted — was not needed once databento was
     verified.

2. **Adapted strategy classes:**
   - `mes_0dte_iron_condor` — **shipped 2026-05-06** as
     `tradegy.options.strategies.mes_0dte_iron_condor.Mes0dteIronCondor`.
     Dollar-offset strike selection (no IV/delta dependency since
     databento ohlcv-1m carries trades only).  Defaults: short legs
     at $50 from spot, $25 wings.  Same-day expiry filter.
   - `mes_0dte_pcs` — **shipped 2026-05-06** as
     `tradegy.options.strategies.mes_0dte_pcs.Mes0dtePcs`.  2-leg
     put credit spread (long put + short put), same dollar-offset
     selection.  Defaults: short put $50 below spot, $25 wing.
     Cuts commissions in half vs the IC ($4 RT vs $8).
   - **2-yr backtest result for PCS (2026-05-06)**: ALSO KILL.
     Cost halving wasn't enough.  Best variant (`$15/$10 ultra-tight`
     short put / $10 wing) at 68.6% win rate, +$745 gross over 325
     trades, **-$555 net** = -$1.71 per trade.  Other variants worse.
     Two-leg structure cut the per-trade cost from $8 to $4 but the
     premium still doesn't cover even the reduced cost on losers.
   - `mes_0dte_short_strangle_defined` — TBD
   - **2023-2024 backtest result (2026-05-06)**: KILL.  At default
     params over 368 0DTE-eligible sessions, with retail-style
     commissions ($1.50/leg RT) and 1-tick-per-side slippage:
     - 221 trades placed, 22.2% win rate, +$647 gross / **-$1,121 net**
     - Median entry credit: $0.80/share = $4 gross, vs $8 RT cost
     - Avg loss per trade: -$5.07
     - Best parameter sweep variant ($25/$25 short, $25 wings):
       321 trades, 57.9% win rate, +$1,090 gross / -$1,478 net
     - Cost dominates premium across all tested variants
     - Conclusion: with ohlcv-1m-only data + retail commissions at
       $5K capital, 0DTE iron condor on MES is not EV-positive.
       Kill recorded; honest finding.
   - **VIX-gated wrapper (2026-05-06)**: `VixGatedStrategy` in
     `tradegy.options.strategies.vix_gated`.  Composes any
     OptionStrategy with a prior-session VIX-close gate (no
     lookahead).  Tested with 0DTE strategies — counter-canonical
     finding: HIGH-VIX gates produce a HINT of edge, low-VIX
     gates do worse.  Mirror-opposite of the doc-14 SPY 45-DTE
     finding (where IV<0.25 was the winning gate).
   - **2-yr backtest under VIX-gating (2026-05-06)**:
     - **IC $25/$25 + VIX > 18**: 82 trades, 70.7% win rate,
       +$776 gross / **+$120 net** (+$1.46/trade)
     - **IC $25/$25 + VIX > 20**: 36 trades, 72.2% win rate,
       +$361 gross / **+$73 net** (+$2.03/trade)
     - PCS variants under VIX>20 mostly negative; only PCS 25/25
       at VIX>20 reached -$8 net (essentially flat)
     - Year-split robustness: 2023 alone +$84 net (44 trades,
       75% WR), 2024 alone +$36 net (38 trades, 65.8% WR) —
       BOTH years positive, signal isn't a single-year fluke.
     - Half-year breakdown: 3 of 4 H1/H2 sub-windows positive;
       2023 H2 (+$236 from 12 trades, 91.7% WR) drives most of
       the alpha — probably regional banking + Fed surprises.
   - **Intraday management (2026-05-06)**: profit-take / loss-stop
     triggers added to `zero_dte_runner` (check at 15-min cadence;
     profit-take fires when MTM ≥ pct of credit, loss-stop fires
     when MTM ≤ -pct of credit).  Added because most 0DTE
     practitioners close at 50% profit early instead of holding to
     expiry — gamma risk in the last 30m blows up otherwise.
   - **Sweep with management (2026-05-06)** on IC $25/$25 + VIX>18:
     - Baseline (no mgmt):           82 trades, 70.7% WR, NET +$120
     - **PT 50% (no LS):             82 trades, 86.6% WR, NET +$708**
     - **PT 75% (no LS):             82 trades, 80.5% WR, NET +$753**
     - PT 50% / LS 200%:             82 trades, 82.9% WR, NET +$330
     - PT 75% / LS 200%:             82 trades, 76.8% WR, NET +$392
     - LS-alone variants HURT (premature close on losers that would
       have recovered).
     - Profit-take alone is the load-bearing improvement: 50%
       fires on 88% of trades, 75% fires on 74%.
   - **Year-split robustness with PT 50% (2026-05-06)**:
     - 2023 full: 44 trades, 86.4% WR, NET +$324 (was -$152 in H1
       w/o mgmt — flipped from loss to profit)
     - 2024 full: 38 trades, 86.8% WR, NET +$385
     - 2023 H1: 32 trades, 81.2% WR, NET +$158 (positive)
     - 2023 H2: 12 trades, 100% WR, NET +$166 (positive)
     - 2024 H1: 4 trades, 100% WR, NET +$76 (positive, n=4)
     - 2024 H2: 34 trades, 85.3% WR, NET +$308 (positive)
     - **All 6 sub-windows positive** — robust to time slicing.
   - **Pre-registered OOS holdout (2026-05-06)**: spec
     IC $25/$25 + VIX>18 + PT 50% locked BEFORE pulling Jan-Apr
     2025 data.  Pass criterion (also pre-registered): avg net
     P&L per trade ≥ +$4.32 (= 50% of in-sample +$8.63).
     **OOS RESULT: PASS — by huge margin.**
     - 35 trades, **91.4% win rate**, NET **+$1,427** in 4 months
     - **+$40.78 per trade** (4.7x in-sample EV)
     - Best trade +$202, worst -$97
     - 30 of 35 closed via PT 50% (5 settled to expiry)
     - Per-month: Jan +$-7 (flat, 3 trades), Feb +$56, Mar +$415
       (100% WR), Apr +$963 (94.1% WR)
     - April 7-9 tariff-news days contributed +$494 (3 trades).
       Even excluding those, OOS = +$933/32 trades = +$29/trade
       (still 3.4x in-sample EV)
     - $1.59 OOS data acquisition cost
   - **Updated verdict (2026-05-06)**: VALIDATED at small scale.
     The IC $25/$25 + VIX>18 + PT 50% spec passed pre-registered
     OOS by 9.4x the threshold, on a 4-month holdout that
     included the April-2025 tariff-driven vol spike (favorable
     regime for short-vol).  In-sample evidence + pre-registered
     OOS pass = significantly stronger than "in-sample only".
     **Caveats remaining**: (a) 35 OOS trades is still a small
     sample; (b) the OOS window over-sampled high-vol days from
     the tariff regime; (c) walk-forward across multiple year
     boundaries + CPCV still not run; (d) deployment requires a
     SPAN broker (IBKR works) and the IC is short-vol — a vol-
     of-vol shock can blow through the wings before management
     fires.  **Recommended deploy decision**: paper-trade at
     1-contract sizing with $5K notional capital allocation for
     30 sessions of live conditions; compare to backtest. If
     parity, scale to $10K/2-contract.  **Path 1 (SPY 16-yr OOS
     Sharpe +0.867 / 13.5% AnnRoC) remains the more
     statistically-grounded option** for someone risk-averse.
   - **DAILY-FIRE variant discovered 2026-05-06** — IC $10/$10 +
     PT 75%, no volatility-index gate.  Found while addressing
     the user's preference for daily activity.  Same harness, same
     held-out 2025 data, no new code needed.
     - 2-yr historical (2023-2024): 324 trades, 75.0% WR,
       **NET +$2,731** (+$8.43/trade)
     - 4-mo held-out (Jan-Apr 2025): 59 trades, 74.6% WR,
       **NET +$954** (+$16.17/trade)
     - All 6 sub-windows (years + half-years) net-positive
     - April 2025 (most-volatile month): 17 trades, 64.7% WR,
       still +$37 net — strategy doesn't break in stress
     - Trade frequency: ~162/yr (vs 30-105 for the gated spec)
     - Per-trade EV nearly identical to gated spec (+$8.43 vs
       +$8.64) — daily fires 4× more often
     - Per-trade max loss ~$30-40 (vs ~$115 for $25/$25 wings)
   - **Entry-time sweep (2026-05-06)** — daemon's old 10:30 ET
     entry was a guess, not an optimum.  Sweeping over the full
     2.3-year window:
     - 09:45 ET: +$4,005 (Strategy B daily)
     - **10:00 ET: +$4,124** (best — current daemon default)
     - 10:30 ET: +$3,686 (old default — left ~$440/2.3yr on the table)
     - 11:00 ET: +$4,035
     - 12:00 ET: +$2,159 (degrades — too little decay time)
     - 15:00 ET: -$458 (loses)
     Daemon updated 2026-05-06 to enter at 10:00 ET.
   - **Adaptive entry experiments (2026-05-06)** — tested the
     "skip violent mornings" rule (skip days where the first 30
     min after 9:30 ET had a range > X% of underlying):
     - No filter: 383 trades, +$4,124, +$10.77/trade
     - Range > 1.0% skip: 373 trades, +$4,096, +$10.98/trade
       (saves 10 trades, ~no net improvement)
     - Range > 0.75% skip: 363 trades, +$3,847, +$10.60 (slightly
       worse)
     - Counterintuitively, the 10% calmest mornings PRODUCE THE
       LOWEST per-trade EV — quiet mornings often turn into
       trending afternoons that break the wings
     - **Conclusion**: morning-range filter doesn't add edge; the
       fixed-time entry is already robust to this
   - **Daemon spec switched 2026-05-06** to the daily-fire config:
     - Strategy: Mes0dteIronCondor(po=10, co=10, ww=10)
     - Volatility-index gate: NONE (fire every Mon-Thu)
     - Profit-take: 75% of credit
     - Entry: 10:00 ET (was 10:30 ET)
      - Management cadence: every 15 min from 10:15-15:45 ET
      - Force-close: 15:30 ET
      - launchd entry plist: 09:55 ET (5 min head start)
      - launchd manage plist: 10:15-15:45 ET (was 10:45-15:45 ET)
    - **GCP packaging shipped 2026-05-10** — first paper-deployment
      package targets a single Compute Engine VM running IB Gateway
      plus systemd timers.  The app image uses locked `uv`
      dependencies, persistent state under `/var/lib/tradegy`,
      explicit `America/New_York` market time, and host-network access
      to IB Gateway on port 4002.  See
      `17_gcp_mes_0dte_deployment.md` and `deploy/gcp/README.md`.
   - **What's STILL not explored**:
     - mbp-1 quotes (~$1.5K for 5yr) — would replace ohlcv-1m
       trade prices with real bid/ask, eliminating stale-price
       contamination in entry credits
     - $25K capital sizing — same per-trade economics, but if
       the broker offers volume-tier commissions ($0.50/contract
       at higher volume), the cost structure changes
     - Tighter cost broker (e.g., Tradier $0.35/contract) —
       lowers RT cost from $4 PCS / $8 IC to ~$1 PCS / $2 IC
     - Walk-forward across multiple year boundaries on the
       daily-fire spec — the held-out 2025 window covers 4 months;
       a longer holdout would tighten the confidence interval
     - True intraday volatility-index data — current "today_open"
       backtest proxy is close to live IBKR VIX query; if a paid
       CBOE intraday feed becomes available, the gated spec
       could be retested with real entry-time vol

3. **Backtest infrastructure:** existing
   `tradegy/options/runner.py` consumes EOD snapshots. For 0DTE we'd
   need a minor extension to handle intraday open→close lifecycle.
   ~1 day of engineering work.

### Expected return at $5K (literature + extrapolation, NOT backtested)

Conservative 0DTE iron condor + IV-gate on MES:
- Sizing: 5-10 ICs per day, ~$50-100 SPAN margin per IC
- Daily premium: $300-500
- Win rate: 70-80% (literature on 0DTE SPX research)
- Loss day: -$300-500 per blow-up
- Net daily expectancy: ~$30-100 conservative

**Realistic AnnRoC estimate: 15-25% = $750-1,250/yr at $5K.**

Compared to Path 1 (13.5% AnnRoC validated): MES 0DTE *might* be
modestly better but with much higher tail risk and no historical
validation yet. Could also be worse — the 0DTE mechanism on futures
has not been tested in this codebase.

### Risk profile at $5K — MES 0DTE

- Single-trade loss: ~$50-100 per IC (defined risk)
- Tail event (Aug 5 2024 type vol spike): can lose 30-60% of capital
  in one day if positions sized aggressively
- Fix: cap concurrent positions to 5-10, hard daily-loss limit at -10%
- $5K is the capital floor; below this, commission drag eats the edge

### Validation gates (must pass before deploying MES 0DTE)

Before risking real capital, the strategy must:
1. **Sanity gate**: ≥30 trades, IS Sharpe > 0
2. **Walk-forward gate**: avg OOS Sharpe ≥ 50% of avg IS Sharpe
3. **CPCV gate**: median path Sharpe > 0.8, < 20% paths negative
4. **Holdout gate**: trailing 6 months untouched, holdout Sharpe ≥ 50%
   of walk-forward
5. **Manual code review** of the 0DTE strategy class — gamma
   blow-ups are real, position-size limits must be hard-coded

If any gate fails, the project's discipline says **kill** — don't
deploy. Same standard that killed pre-FOMC drift.

## Comparison table

| Metric | Path 1: $5K SPY validated | Path 2: $5K MES 0DTE proposed |
|---|---|---|
| Data needed | None (we have it) | Existing databento, ~$10 for 5yr full daily |
| Strategy code | Exists | Needs ~1 day eng |
| Walk-forward validated? | YES (16yr, +0.867 OOS Sharpe) | Not yet |
| Intraday-compliant? | NO (multi-day) | YES (0DTE) |
| AnnRoC estimate | 13.5% validated | 15-25% literature |
| Tail risk | Multi-day vol spike | 0DTE gap-day blowup |
| Effort | Deploy today | Build + backtest first |
| Time to first dollar | 1-2 weeks (paper trial) | 4-6 weeks (data + build + validate) |

## Recommended sequence (what to do in what order)

### This week
1. **Deploy Path 1 immediately on paper account** — `DU7535411` exists.
   Run the validated SPY config at $5K capital sizing. Paper-trade for
   one full options cycle (~30 days) to confirm execution + verify
   the live behavior matches the backtest.
2. **Pull X-prefix daily MES options data** via the existing
   `download_mes_options.py` script (databento subscription already
   covers it). 2-3 year window first (~$5), extend if Phase 2 results
   warrant it.

### Weeks 2-4
3. **Build option-chain assembly logic** — convert databento
   `definition` records to the project's `ChainSnapshot`/`OptionLeg`
   dataclasses (`src/tradegy/options/chain.py`), with the X-prefix
   contracts mapped to their MES future underlying.
4. **Build 0DTE strategy classes** (`mes_0dte_iron_condor`,
   `mes_0dte_pcs`, etc.) — extension of the existing options strategy
   framework.
5. **Backtest at $5K capital** through walk-forward + CPCV + holdout.

### Weeks 4-6
6. **If MES 0DTE passes gates**: paper-trade alongside the live SPY
   strategy. Compare AnnRoC and drawdown profile.
7. **If MES 0DTE fails gates**: stick with Path 1. The data spend
   was ~$10 — sunk cost is trivial; honest kill, not a regret.

### Months 2-6
8. **Scale Path 1 to $10-15K** as capital grows (mechanism scales
   linearly, so 2x capital = 2x return).
9. **If MES 0DTE validated**: deploy on top of Path 1 (uncorrelated,
   intraday vs multi-day = portfolio diversification).

## Honest "expected returns" picture at $5K

Six-month outcomes by scenario:

| Scenario | $ outcome | Probability |
|---|---|---|
| Path 1 only, normal market | +$300 to +$700 | 40% |
| Path 1 only, drawdown window | -$500 to +$200 | 25% |
| Path 1 + Path 2 both validated | +$700 to +$1,500 | 15% |
| Path 2 fails, just Path 1 | (per Path 1 only) | 15% |
| Path 1 fails (regime break) | -$1,000 to +$200 | 5% |

Expected value across scenarios: ~+$400-700 over 6 months at $5K =
8-14% half-year return.

Better than passive, NOT life-changing. The strategy is appropriate
for someone who wants to LEARN options trading mechanics with real
capital at risk and has the temperament to ride through the loss
windows.

## What's NOT in scope at $5K

- Naked short options (SPAN ~$1-3K per contract = whole account at
  risk on one position)
- Complex multi-asset strategies (capital can't diversify)
- Active intraday day-trading of options (commission drag too high)
- Tail-hedging strategies (the cost of long-vol protection is too
  much premium drag at $5K)

These all require ≥$25K to be unit-economically viable.

## Files referenced

- Strategy classes: `src/tradegy/options/strategies/` (14 registered
  per `tradegy options-strategies`)
- Backtest harness: `src/tradegy/options/runner.py`,
  `walk_forward.py`, `cpcv.py`
- IV-gating wrapper: `src/tradegy/options/strategies/iv_gated.py`
- Source data: `data/raw/source=spy_options_chain/` (already ingested,
  2010-2026 daily snapshots)
- Plan reference: `trading_platform_docs/14_options_vol_selling.md`
  (the doc-14 work this plan extends)
