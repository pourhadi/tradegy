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
   - `mes_0dte_iron_condor` — same as `iron_condor_45dte_d16` but on
     MES, single-day expiry, sized for $5K SPAN
   - `mes_0dte_pcs` — put credit spread, 0DTE
   - `mes_0dte_short_strangle_defined` — sells ATM both sides
   - All wrapped with the IV-gate (same `iv_gated_max=0.25` from Path 1)

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
