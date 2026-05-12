# MES Intraday Directional Research Plan

**Status:** Fourth executable batch (broader high-importance event pool) killed at walk-forward, 2026-05-11.  
**Purpose:** Define the next disciplined attempt to find profitable MES intraday trades without repeating the killed MES-only, price-only search.

## Thesis

Prior MES directional variants failed because trade selection was too weak. The next attempt must require independent context before a trade can fire.

Working premise:

```text
MES intraday edge = scheduled macro context + vol regime + cross-market confirmation
```

Rejected premise:

```text
MES intraday edge = another standalone MES technical indicator
```

Every strategy in this lane must have at least two independent gates unless the mechanism is a documented scheduled-event anomaly with naturally sparse trade count.

## Current Inventory

Already available in this repo:

| Input | Current artifact | Notes |
|---|---|---|
| MES 1m bars | `mes_1m_ohlcv`, `mes_1m_bars` | Core tradable stream. |
| SPY 1m bars | `spy_1m_ohlcv`, `spy_1m_bars` | Cash-market proxy and cross-check. |
| ES/NQ/RTY/YM/MNQ/M2K 1m bars | `es_1m_ohlcv`, `nq_1m_ohlcv`, `rty_1m_ohlcv`, `ym_1m_ohlcv`, `mnq_1m_ohlcv`, `m2k_1m_ohlcv` | Cross-index divergence and participation inputs. ES/NQ/RTY/YM include Sierra bid/ask volume. |
| VIX daily | `vix_daily_close`, `vix_daily_pctile_252`, `vix_daily_5d_change` | Regime level and vol-compression/expansion direction. |
| VX 1m futures | `vx_1m_ohlcv` | Intraday CFE VIX futures confirmation from Sierra Chart SCID files. Non-back-adjusted front-month continuous series. |
| Rates 1m futures | `zt_1m_ohlcv`, `zf_1m_ohlcv`, `zn_1m_ohlcv`, `zb_1m_ohlcv` | 2Y, 5Y, 10Y, and 30Y Treasury futures confirmation. ZF/ZB include Sierra bid/ask volume; ZT/ZN currently use Databento OHLCV. |
| CL 1m futures | `cl_1m_ohlcv`, `cl_1m_bars` | WTI crude oil macro-shock input from Sierra Chart SCID. Valid through 2026-04-21 with April 2020 negative prices explicitly admitted via source-level audit floor. |
| GC 1m futures | `gc_1m_ohlcv`, `gc_1m_bars` | COMEX gold safe-haven/real-rate shock input from Sierra Chart SCID. Valid through 2026-04-30. |
| Macro events | `econ_events`, hours-to-next-event features | Supports pre-event drift and quiet-window gates. |
| OR30 / VWAP / prior close | `mes_or30_high`, `mes_or30_low`, `mes_vwap`, `mes_prior_rth_close` | Existing intraday anchors. |

Not yet available and therefore not used in the executable first batch:

| Needed input | Why it matters | Required before testing |
|---|---|---|
| Intraday VIX/VX | True intraday vol confirmation and VIX divergence. | Unblocked for VX futures via `vx_1m_ohlcv`; cash VIX intraday remains separate. |
| Breadth / ADD / TICK / sector breadth | EOD continuation needs market-wide participation, not just index price. | Admit a breadth source with live/historical parity. |
| SPX/SPY gamma surface | 0DTE gamma pin / flip-zone research. | Build point-in-time chain-derived gamma exposure features. |
| DXY | Macro continuation confirmation. | Admit DXY or a liquid futures proxy if dollar confirmation becomes necessary. |

Newly added from existing data on 2026-05-10:

| Feature | Raw data needed? | Notes |
|---|---|---|
| `mes_hours_since_last_cpi` | No | Built from `econ_events` + `mes_1m_bars`; no-lookahead/reproducibility passed 50/50. |
| `mes_cpi_30m_reaction_return` | No | First 30m signed MES return after CPI; emits only after the 30m reaction window completes. |
| `mes_fomc_30m_reaction_return` | No | First 30m signed MES return after FOMC statements; emits only after the 30m reaction window completes. |
| `vx_1m_ohlcv` | Sierra Chart SCID | Ingested 2026-05-10 from CFE VX contract files; audit passed with no findings. |
| `zn_1m_ohlcv` | Databento | Downloaded/ingested 2026-05-10 from `GLBX.MDP3`; audit passed with no findings after excluding calendar-spread rows. |
| `zt_1m_ohlcv` | Databento | Downloaded/ingested 2026-05-10 from `GLBX.MDP3`; audit passed with no findings after excluding calendar-spread rows. |
| `vx_5m_log_returns` | No | Built from admitted VX SCID source; no-lookahead/reproducibility passed 25/25. |
| `zn_5m_log_returns` | No | Built from admitted ZN source; no-lookahead/reproducibility passed 25/25. |
| `zt_5m_log_returns` | No | Built from admitted ZT source; no-lookahead/reproducibility passed 25/25. |
| `zn_zt_curve_5m_change` | No | Built from admitted ZN/ZT sources; no-lookahead/reproducibility passed 25/25. |

Newly added from existing data on 2026-05-11:

| Feature | Raw data needed? | Notes |
|---|---|---|
| `mes_hours_since_last_high_event` | No | Combined-event variant: hours since the last high-importance event (fomc_statement, fomc_sep, cpi, employment_situation, gdp). Built from `econ_events` + `mes_1m_bars`; no-lookahead/reproducibility passed 200/200. |
| `mes_high_event_30m_reaction_return` | No | Combined-event variant: signed MES return 30m after a high-importance event. 294 distinct reaction-return values materialized 2019-2026 (58 with reaction <= -0.20%, 77 with reaction >= +0.20%). No-lookahead/reproducibility passed 200/200. |
| `nq_1m_ohlcv` | Sierra Chart SCID | Ingested after Sierra backfill; audit passed with no findings over 2019-05-06 → 2026-04-30. |
| `nq_1m_bars` | No | Built from admitted `nq_1m_ohlcv`; no-lookahead/reproducibility passed 50/50. |
| `es_1m_ohlcv` | Sierra Chart SCID | Ingested after Sierra backfill; audit passed with no findings over 2019-05-06 → 2026-04-30. |
| `es_1m_bars` | No | Switched to admitted `es_1m_ohlcv`; no-lookahead/reproducibility passed 50/50. |
| `rty_1m_ohlcv` | Sierra Chart SCID | Ingested from Sierra quarterly RTY files; audit passed with no findings over 2019-05-06 → 2026-04-30. |
| `rty_1m_bars` | No | Built from admitted `rty_1m_ohlcv`; no-lookahead/reproducibility passed 50/50. |
| `ym_1m_ohlcv` | Sierra Chart SCID | Switched from Databento OHLCV to Sierra quarterly YM files; audit passed with no findings over 2019-05-06 → 2026-04-30. |
| `ym_1m_bars` | No | Rebuilt from admitted Sierra `ym_1m_ohlcv`; no-lookahead/reproducibility passed 50/50. |
| `zf_1m_ohlcv` | Sierra Chart SCID | Ingested from Sierra quarterly ZF files; audit passed with no findings over 2019-05-06 → 2026-04-30. |
| `zf_1m_bars` | No | Built from admitted `zf_1m_ohlcv`; no-lookahead/reproducibility passed 50/50. |
| `zb_1m_ohlcv` | Sierra Chart SCID | Ingested from Sierra quarterly ZB files; audit passed with no findings over 2019-05-06 → 2026-04-30. |
| `zb_1m_bars` | No | Built from admitted `zb_1m_ohlcv`; no-lookahead/reproducibility passed 50/50. |
| `cl_1m_ohlcv` | Sierra Chart SCID | Ingested from Sierra monthly CL files; audit passed after declaring `minimum_price: -100.0` for the April 2020 WTI negative-price episode. Observed coverage is 2019-05-06 → 2026-04-21 because `CLM26-NYMEX.scid` is not present locally. |
| `cl_1m_bars` | No | Built from admitted `cl_1m_ohlcv`; no-lookahead/reproducibility passed 50/50. |
| `gc_1m_ohlcv` | Sierra Chart SCID | Ingested from Sierra COMEX GC active-month files (`G`, `J`, `M`, `Q`, `Z`); audit passed with no findings over 2019-05-06 → 2026-04-30. |
| `gc_1m_bars` | No | Built from admitted `gc_1m_ohlcv`; no-lookahead/reproducibility passed 50/50. |

## Advanced Next Steps

The simple threshold-confirmation path is exhausted for now. The failed batches say that `VX up + rates up` and related hand-written gates are not enough; they do not prove that VX, rates, NQ, breadth, or other futures contain no MES-selection information.

The next research lane should move from direct entry rules to selection and state modeling:

| Lane | Core question | Concrete next work | Discipline gate |
|---|---|---|---|
| Meta-labeling | Given a pre-existing MES setup, should this candidate trade be taken? | Build an event table for OR30 breaks, VWAP deviations, prior-close gaps, and post-event first moves; label each candidate by whether it reaches +1R before -1R or has positive fixed-horizon R. | Purged walk-forward only; model must beat always-take and never-take baselines. |
| Cross-market regime classifier | Which market state makes each setup viable? | Cluster or classify regimes from MES/NQ/MNQ/RTY/YM, VX, ZN/ZT/ZF/ZB, and optional CL/GC/FX features: returns, vol, correlation, curve slope, dispersion, and time-of-day. | Regime must be stable across walk-forward windows and cannot be selected post-hoc per failed strategy. |
| Lead/lag forecasting | Do other futures lead MES at specific horizons? | Model forward MES 5m/15m/30m/60m returns from lagged cross-market returns, vol, correlation, and interaction terms. Start with ridge/logistic before tree models. | No random splits; use embargoed/purged CV because labels overlap. |
| Term-structure features | Is the signal in curve shape rather than single-contract movement? | Add VX M1/M2 curve movement and ZT/ZF/ZN/ZB curve slope/twist features. Use relative changes, not non-back-adjusted absolute levels. | Feature validation first; no strategy backtest until term-structure features are pre-registered. |
| Microstructure / participation | Does signed flow identify false breaks or real participation? | Use Sierra SCID `num_trades`, `bid_volume`, and `ask_volume` for NQ and other futures to build cumulative delta, bid/ask imbalance, and cross-market flow-divergence features. | Must pass no-lookahead and be evaluated as candidate-trade filters, not as free-running every-minute predictors. |
| Event surprise modeling | Are CPI/FOMC/NFP reactions explainable by the actual surprise? | Add actual-vs-consensus surprise fields for CPI, payrolls, unemployment/wages, and Fed decisions/dots; pair with ZT/ZN/VX reaction features. | Event taxonomy and surprise fields must be fixed before testing. |

Recommended implementation order:

1. Admit Sierra NQ SCID first because it is now locally available and adds full E-mini Nasdaq price plus bid/ask volume fields.
2. Generalize the Sierra SCID ingest path for quarterly CME futures so NQ, ES, RTY/YM, ZF/ZN/ZB, CL, GC, and FX futures can share one ingestion contract.
3. Build a candidate-trade table for existing MES setups before building ML models.
4. Train only selection models over candidate trades: logistic/ridge first, then LightGBM/XGBoost only if linear baselines show signal.
5. Evaluate with purged walk-forward and a locked holdout; kill models that cannot beat simple baselines after costs.

### Sierra NQ SCID Admission

Status on 2026-05-11: admitted after Sierra backfilled the local quarterly SCID files.

What shipped:

| Artifact | Status | Notes |
|---|---|---|
| Generic Sierra futures SCID parser | Implemented | Supports quarterly dash-named contracts such as `NQM24-CME.scid` in addition to legacy VX monthly files. |
| `nq_1m_ohlcv` registry entry | Admitted | Marked `revision_policy: never_revised` after audit passed. |
| Local NQ ingest | Passed | Batch `944c8e5b8d9b616a`; coverage observed `2019-05-06 00:00:00 UTC` to `2026-04-30 23:59:00 UTC`. |
| `nq_1m_bars` feature | Passed validation | 2,466,721 rows; no-lookahead/reproducibility passed 50/50. |

Audit result:

| Check | Result |
|---|---|
| Unique 1-minute timestamps | 2,466,721 |
| Years with rows | 2019-2026 research window |
| Largest unexpected gap | None reported by source audit |
| Non-positive open issue | Fixed in parser by treating Sierra unset-open sentinel as missing and using close for open while preserving valid high/low. |
| Admission | PASS: `uv run tradegy audit nq_1m_ohlcv` returned no findings. |

Decision: use `nq_1m_ohlcv` as the admitted NQ source for cross-market regime, dispersion, and participation features. The generic parser can be reused for other Sierra futures, but every new source still needs its own audit before admission.

### Sierra ES SCID Admission

Status on 2026-05-11: admitted after Sierra backfilled the 2026 ES quarterly files.

| Metric | Value |
|---|---:|
| Raw SCID records parsed inside coverage window | 1,803,876,952 |
| Continuous 1m rows | 2,466,841 |
| Overlapping contract-minutes dropped | 345,381 |
| Coverage start | 2019-05-06 00:00:00 UTC |
| Coverage end | 2026-04-30 23:59:00 UTC |
| Batch id | `dc0dd0e244165527` |
| Audit | Pass: no findings |

Feature validation:

| Feature | Rows | Validation |
|---|---:|---|
| `es_1m_bars` | 2,466,841 | no-lookahead PASS 50/50; reproducibility PASS |

Decision: use `es_1m_ohlcv` and `es_1m_bars` as the preferred parent-market participation source for MES candidate-trade filtering and meta-labeling. ES has deeper liquidity than MES and carries Sierra `num_trades`, `bid_volume`, and `ask_volume`, making it suitable for cumulative-delta and flow-divergence features.

### Sierra RTY SCID Admission

Status on 2026-05-11: admitted from local Sierra quarterly SCID files.

| Metric | Value |
|---|---:|
| Raw SCID records parsed inside coverage window | 266,592,896 |
| Continuous 1m rows | 2,390,015 |
| Overlapping contract-minutes dropped | 219,937 |
| Coverage start | 2019-05-06 00:00:00 UTC |
| Coverage end | 2026-04-30 23:59:00 UTC |
| Batch id | `14076047e8a3e57d` |
| Audit | Pass: no findings |

Feature validation:

| Feature | Rows | Validation |
|---|---:|---|
| `rty_1m_bars` | 2,390,015 | no-lookahead PASS 50/50; reproducibility PASS |

Decision: use `rty_1m_ohlcv` and `rty_1m_bars` as the admitted small-cap breadth/dispersion input for MES candidate-trade filtering and equity-index regime classification.

### Sierra YM SCID Admission

Status on 2026-05-11: switched `ym_1m_ohlcv` from Databento OHLCV to Sierra Chart quarterly SCID files to add participation fields.

| Metric | Value |
|---|---:|
| Raw SCID records parsed inside coverage window | 264,550,173 |
| Continuous 1m rows | 2,451,476 |
| Overlapping contract-minutes dropped | 267,671 |
| Coverage start | 2019-05-06 00:00:00 UTC |
| Coverage end | 2026-04-30 23:59:00 UTC |
| Batch id | `dcdd2e70d7d6f7d6` |
| Audit | Pass: no findings |

Feature validation:

| Feature | Rows | Validation |
|---|---:|---|
| `ym_1m_bars` | 2,451,476 | no-lookahead PASS 50/50; reproducibility PASS |

Decision: use Sierra `ym_1m_ohlcv` and `ym_1m_bars` as the admitted Dow/value/industrials participation input for MES candidate-trade filtering and equity-index regime classification.

### Sierra ZF SCID Admission

Status on 2026-05-11: admitted from local Sierra quarterly SCID files.

| Metric | Value |
|---|---:|
| Raw SCID records parsed inside coverage window | 356,537,090 |
| Continuous 1m rows | 2,267,004 |
| Overlapping contract-minutes dropped | 280,829 |
| Coverage start | 2019-05-06 00:00:00 UTC |
| Coverage end | 2026-04-30 23:59:00 UTC |
| Batch id | `7ab01cff647fd02d` |
| Audit | Pass: no findings |

Feature validation:

| Feature | Rows | Validation |
|---|---:|---|
| `zf_1m_bars` | 2,267,004 | no-lookahead PASS 50/50; reproducibility PASS |

Parser note: the ZF audit initially surfaced 2 rows with non-positive low values caused by Sierra unset-price values. The SCID normalizer now treats non-positive high/low as missing for that record and uses close as the fallback, matching the existing unset-open handling.

Decision: use `zf_1m_ohlcv` and `zf_1m_bars` as the admitted 5-year Treasury belly input for curve-shock classification and MES candidate-trade filtering.

### Sierra ZB SCID Admission

Status on 2026-05-11: admitted from local Sierra quarterly SCID files.

| Metric | Value |
|---|---:|
| Raw SCID records parsed inside coverage window | 234,000,125 |
| Continuous 1m rows | 2,208,951 |
| Overlapping contract-minutes dropped | 253,989 |
| Coverage start | 2019-05-06 00:00:00 UTC |
| Coverage end | 2026-04-30 23:59:00 UTC |
| Batch id | `315bf24c6eb82afb` |
| Audit | Pass: no findings |

Feature validation:

| Feature | Rows | Validation |
|---|---:|---|
| `zb_1m_bars` | 2,208,951 | no-lookahead PASS 50/50; reproducibility PASS |

Decision: use `zb_1m_ohlcv` and `zb_1m_bars` as the admitted 30-year Treasury long-end input for curve-shock classification and MES candidate-trade filtering.

### Sierra CL SCID Admission

Status on 2026-05-12: admitted from local Sierra monthly NYMEX CL files with an explicit negative-price audit floor.

| Metric | Value |
|---|---:|
| Raw SCID records parsed inside coverage window | 448,451,393 |
| Continuous 1m rows | 2,455,442 |
| Overlapping contract-minutes dropped | 1,318,762 |
| Coverage start | 2019-05-06 00:00:00 UTC |
| Coverage end | 2026-04-21 18:29:00 UTC |
| Batch id | `bf204e83f177f431` |
| Audit | Pass: no findings after source-level `minimum_price: -100.0` policy |

Feature validation:

| Feature | Rows | Validation |
|---|---:|---|
| `cl_1m_bars` | 2,455,442 | no-lookahead PASS 50/50; reproducibility PASS |

Audit note: the first CL audit flagged 358 non-positive price rows from `CLK20-NYMEX` between 2020-04-20 18:08 UTC and 2020-04-21 13:25 UTC. Those rows line up with the April 2020 WTI negative-price episode, so the data-source schema now supports a source-level `minimum_price` floor. CL declares `minimum_price: -100.0`; other sources remain strictly positive by default.

Coverage note: local Sierra files currently stop at `CLK26-NYMEX.scid`. `CLM26-NYMEX.scid` is not present, so CL does not currently cover 2026-04-22 through 2026-04-30. Do not align CL-dependent models against the full April 2026 endpoint until the missing June 2026 contract file is added and the source is reingested/audited.

Decision: use `cl_1m_ohlcv` and `cl_1m_bars` as the admitted energy/geopolitical shock input for regime classification and MES candidate-trade meta-labeling, with the shorter coverage window explicitly respected.

### Sierra GC SCID Admission

Status on 2026-05-12: admitted from local Sierra COMEX GC active-month files.

| Metric | Value |
|---|---:|
| Raw SCID records parsed inside coverage window | 296,421,529 |
| Continuous 1m rows | 2,474,711 |
| Overlapping contract-minutes dropped | 479,492 |
| Coverage start | 2019-05-06 00:00:00 UTC |
| Coverage end | 2026-04-30 23:59:00 UTC |
| Batch id | `79205735f2f39dd4` |
| Audit | Pass: no findings |

Feature validation:

| Feature | Rows | Validation |
|---|---:|---|
| `gc_1m_bars` | 2,474,711 | no-lookahead PASS 50/50; reproducibility PASS |

Parser note: GC required a third SCID contract-cycle option, `gold_active`, covering the COMEX active months February, April, June, August, and December. Local files cover `GCG18` through `GCM26`; the admitted research window uses the files needed for 2019-05-06 through 2026-04-30.

Decision: use `gc_1m_ohlcv` and `gc_1m_bars` as the admitted gold/safe-haven and real-rate-sensitive input for regime classification and MES candidate-trade meta-labeling.

### Sierra NQ Bar Export Attempt

After the SCID-directory audit failed, a chart bar-data export was tested:

```text
/Users/dan/Applications/Kegworks/sierrachart.app/Contents/SharedSupport/prefix/drive_c/Program Files/SierraChart2739/Data/NQ-CME.scid_BarData.txt
```

Result on 2026-05-11:

| Metric | Value |
|---|---:|
| Rows ingested | 43,619,910 |
| Unique timestamps | 43,619,910 |
| Coverage start | 2011-02-25 01:22:29 UTC |
| Coverage end | 2026-05-11 20:28:45 UTC |
| Batch id | `836d953ba500aeee` |
| Audit | FAIL: 4 unexpected gaps > 604,800s; largest ~1,985 days |

Year distribution:

| Year | Rows |
|---:|---:|
| 2011 | 4,286,815 |
| 2012 | 4,684,310 |
| 2013 | 4,489,019 |
| 2014 | 5,281,845 |
| 2015 | 6,021,495 |
| 2016 | 6,357,716 |
| 2017 | 4,613,067 |
| 2018 | 7,421 |
| 2020 | 5,747,697 |
| 2026 | 2,130,525 |

Decision: the export proves the Sierra bar-data path is parseable, but the SCID-directory path is now cleaner and audit-passing after backfill. Keep `nq_sierra_ohlcv` as `not_admitted`; use admitted `nq_1m_ohlcv` and derived `nq_1m_bars` for research.

## First Executable Batch

The first batch intentionally uses only existing registered features and strategy classes. It is not tuned from results; parameters below are pre-registered.

| Spec | Mechanism | Independent gates | Status |
|---|---|---|---|
| `mes_vix_confirmed_or30_breakout` | OR30 continuation when high/expanding VIX suggests real directional repricing. | OR30 break with volume, VIX percentile > 0.50, VIX 5d change > 0, event quiet, early/mid RTH window. | Killed at sanity. |
| `mes_vix_falling_gap_fade` | Overnight gap mean reversion when vol is compressing and no macro catalyst is active. | Gap threshold, VIX percentile < 0.50, VIX 5d change < 0, event quiet, early RTH window. | Killed at sanity. |
| `mes_mnq_divergence_fade_long` | MES cheap vs MNQ mean reverts as index-arb pressure closes the spread. | MES/MNQ z < -2, RTH time gate, event quiet. | Killed at sanity. |
| `mes_mnq_divergence_fade_short` | MES rich vs MNQ mean reverts as index-arb pressure closes the spread. | MES/MNQ z > +2, RTH time gate, event quiet. | Killed at sanity. |

## Second Executable Batch

The second batch targets post-event first-move overreaction using the new point-in-time reaction features. It still uses no new vendor data.

| Spec | Mechanism | Independent gates | Status |
|---|---|---|---|
| `mes_post_cpi_first_move_fade_long` | Fade sharp downside CPI first move after the 30m reaction window completes. | Hours since CPI 0.5-2.0, CPI 30m reaction <= -0.20%. | Killed at sanity: positive but underpowered. |
| `mes_post_cpi_first_move_fade_short` | Fade sharp upside CPI first move after the 30m reaction window completes. | Hours since CPI 0.5-2.0, CPI 30m reaction >= +0.20%. | Killed at sanity. |
| `mes_post_fomc_first_move_fade_long` | Fade sharp downside FOMC first move after the 30m reaction window completes. | Hours since FOMC 0.5-2.0, FOMC 30m reaction <= -0.20%. | Killed at sanity: positive but underpowered. |
| `mes_post_fomc_first_move_fade_short` | Fade sharp upside FOMC first move after the 30m reaction window completes. | Hours since FOMC 0.5-2.0, FOMC 30m reaction >= +0.20%. | Killed at sanity: positive but underpowered. |

## Second Batch Results

Single-window sanity backtests were run immediately after pre-registration on 2026-05-10 with the standard futures cost model.

| Spec | Trades | PnL | Per-trade Sharpe | Result |
|---|---:|---:|---:|---|
| `mes_post_cpi_first_move_fade_long` | 21 | +$97.75 | +0.205 | Fails sanity: fewer than 30 trades. |
| `mes_post_cpi_first_move_fade_short` | 26 | -$78.88 | -0.159 | Fails sanity: fewer than 30 trades and negative Sharpe. |
| `mes_post_fomc_first_move_fade_long` | 3 | +$26.88 | +0.287 | Fails sanity: far fewer than 30 trades. |
| `mes_post_fomc_first_move_fade_short` | 6 | +$140.88 | +0.362 | Fails sanity: far fewer than 30 trades. |

Evidence packets:

| Spec | Evidence packet |
|---|---|
| `mes_post_cpi_first_move_fade_long` | `data/evidence/mes_post_cpi_first_move_fade_long__backtest__20260510T063158.json` |
| `mes_post_cpi_first_move_fade_short` | `data/evidence/mes_post_cpi_first_move_fade_short__backtest__20260510T063158.json` |
| `mes_post_fomc_first_move_fade_long` | `data/evidence/mes_post_fomc_first_move_fade_long__backtest__20260510T063159.json` |
| `mes_post_fomc_first_move_fade_short` | `data/evidence/mes_post_fomc_first_move_fade_short__backtest__20260510T063158.json` |

Interpretation: first-move fade may be directionally interesting for downside CPI and FOMC, but the pre-registered gates do not produce enough trades over the available 2019-2026 MES sample to clear the system's minimum-evidence bar. Expanding event types or loosening thresholds after seeing these results would be post-hoc. Future work should either add more historical MES coverage, test a separately pre-registered broader high-importance-event pool, or move to options/gamma features where event frequency is not the bottleneck.

Existing specs remain part of the broader evidence map, especially `mes_pre_fomc_drift`, `mes_pre_high_event_drift_combined`, `mes_mvp_vwap_reversion`, `mes_mvp_range_break_continuation`, `mes_mvp_gap_fill_fade`, and `mes_eod_long`. The four specs above are the new cross-domain core for this research round.

## Third Executable Batch

The third batch is the first one to use admitted intraday VX and rates futures. Thresholds were selected from unconditional feature quantiles before any strategy backtest, not from PnL:

| Feature | Approx gate | Rationale |
|---|---:|---|
| `vx_5m_log_returns` | +/-0.006 | About the 10th/90th percentile of all materialized 5m VX returns. |
| `zn_5m_log_returns` | +/-0.00025 | About the 10th/90th percentile of all materialized 5m ZN returns. |
| `zt_5m_log_returns` | +/-0.00007 | About the 10th/90th percentile of all materialized 5m ZT returns. |

Pre-registered specs:

| Spec | Mechanism | Independent gates | Status |
|---|---|---|---|
| `mes_vx_rates_or30_breakout_short` | OR30 downside continuation when vol and Treasuries confirm risk-off. | Lower OR30 break with volume, VX 5m > 0.006, ZN 5m > 0.00025, ZT 5m > 0.00007, early/mid RTH. | Killed at sanity. |
| `mes_vx_rates_or30_breakout_long` | OR30 upside continuation when vol and Treasuries confirm risk-on. | Upper OR30 break with volume, VX 5m < -0.006, ZN 5m < -0.00025, ZT 5m < -0.00007, early/mid RTH. | Killed at sanity. |
| `mes_cpi_vx_rates_downside_continuation_short` | Continue a negative CPI first move only when VX/rates confirm risk-off. | Hours since CPI 0.5-2.0, CPI reaction <= -0.20%, VX 5m > 0.006, ZN 5m > 0.00025. | Killed at sanity. |
| `mes_fomc_vx_rates_downside_continuation_short` | Continue a negative FOMC first move only when VX/rates confirm risk-off. | Hours since FOMC 0.5-2.0, FOMC reaction <= -0.20%, VX 5m > 0.006, ZN 5m > 0.00025. | Killed at sanity. |
| `mes_cpi_unconfirmed_downside_fade_long` | Fade a negative CPI first move only when VX/rates do not confirm risk-off. | Hours since CPI 0.5-2.0, CPI reaction <= -0.20%, VX 5m < 0.006, ZN 5m < 0.00025. | Killed at sanity: positive but underpowered. |
| `mes_fomc_unconfirmed_downside_fade_long` | Fade a negative FOMC first move only when VX/rates do not confirm risk-off. | Hours since FOMC 0.5-2.0, FOMC reaction <= -0.20%, VX 5m < 0.006, ZN 5m < 0.00025. | Killed at sanity: positive but underpowered. |

## Third Batch Results

Single-window sanity backtests were run immediately after pre-registration on 2026-05-11 with the standard futures cost model.

| Spec | Trades | PnL | Per-trade Sharpe | Result |
|---|---:|---:|---:|---|
| `mes_vx_rates_or30_breakout_short` | 185 | -$268.57 | -0.172 | Fails sanity: negative Sharpe. |
| `mes_vx_rates_or30_breakout_long` | 93 | -$285.25 | -0.422 | Fails sanity: negative Sharpe. |
| `mes_cpi_vx_rates_downside_continuation_short` | 11 | -$53.00 | -0.227 | Fails sanity: fewer than 30 trades and negative Sharpe. |
| `mes_fomc_vx_rates_downside_continuation_short` | 1 | -$26.62 | 0.000 | Fails sanity: far fewer than 30 trades. |
| `mes_cpi_unconfirmed_downside_fade_long` | 21 | +$98.00 | +0.208 | Fails sanity: fewer than 30 trades. |
| `mes_fomc_unconfirmed_downside_fade_long` | 3 | +$26.88 | +0.287 | Fails sanity: far fewer than 30 trades. |

Evidence packets:

| Spec | Evidence packet |
|---|---|
| `mes_vx_rates_or30_breakout_short` | `data/evidence/mes_vx_rates_or30_breakout_short__backtest__20260511T045118.json` |
| `mes_vx_rates_or30_breakout_long` | `data/evidence/mes_vx_rates_or30_breakout_long__backtest__20260511T045118.json` |
| `mes_cpi_vx_rates_downside_continuation_short` | `data/evidence/mes_cpi_vx_rates_downside_continuation_short__backtest__20260511T045116.json` |
| `mes_fomc_vx_rates_downside_continuation_short` | `data/evidence/mes_fomc_vx_rates_downside_continuation_short__backtest__20260511T045116.json` |
| `mes_cpi_unconfirmed_downside_fade_long` | `data/evidence/mes_cpi_unconfirmed_downside_fade_long__backtest__20260511T045116.json` |
| `mes_fomc_unconfirmed_downside_fade_long` | `data/evidence/mes_fomc_unconfirmed_downside_fade_long__backtest__20260511T045116.json` |

Interpretation: simple intraday VX/rates confirmation did not rescue MES OR30 continuation; both directions had adequate sample and negative Sharpe. Event-continuation variants were too sparse and negative. The CPI/FOMC unconfirmed downside fade remains directionally positive, but still under the 30-trade minimum and cannot be promoted without a separately pre-registered broader event pool or more history.

Run order:

```bash
uv run tradegy backtest mes_vx_rates_or30_breakout_short
uv run tradegy backtest mes_vx_rates_or30_breakout_long
uv run tradegy backtest mes_cpi_vx_rates_downside_continuation_short
uv run tradegy backtest mes_fomc_vx_rates_downside_continuation_short
uv run tradegy backtest mes_cpi_unconfirmed_downside_fade_long
uv run tradegy backtest mes_fomc_unconfirmed_downside_fade_long
```

## Fourth Executable Batch

The fourth batch is the doc-16-sanctioned response to the batch-2 underpowered-but-directionally-positive finding. The event pool is broadened from {cpi, fomc_statement} to the canonical high-importance set as classified by the admitted `econ_events` source as of 2026-05-11: `fomc_statement`, `fomc_sep`, `cpi`, `employment_situation` (NFP), `gdp`. The high-importance pool contains 303 events between 2019-05 and 2026-04.

Every numeric parameter (reaction_minutes, reaction_return_abs_min, hours_since_window, max_holding_bars, stop_ticks) is locked identically to the batch-2 CPI/FOMC specs. This is a pure event-set generalization test, not a parameter re-search. Pre-registration locks no parameter expansion after observing results.

Pre-registered specs:

| Spec | Mechanism | Independent gates | Status |
|---|---|---|---|
| `mes_post_high_event_first_move_fade_long` | Fade sharp downside first move after any high-importance event after the 30m reaction window completes. | Hours since high-importance event 0.5-2.0, high-event 30m reaction <= -0.20%. | Sanity PASS, walk-forward FAIL. Killed. |
| `mes_post_high_event_first_move_fade_short` | Fade sharp upside first move after any high-importance event after the 30m reaction window completes. | Hours since high-importance event 0.5-2.0, high-event 30m reaction >= +0.20%. | Sanity PASS, walk-forward FAIL. Killed. |

Projected qualifying events from the materialized 2019-2026 feature distribution (pre-sanity): 58 events with reaction <= -0.20% (long-fade), 77 events with reaction >= +0.20% (short-fade). Both comfortably above the 30-trade sanity bar before any session/holding-time filtering.

Hypothesis: `hyp_mes_post_high_event_first_move_fade_20260511`. Parent: `hyp_mes_post_event_first_move_fade_20260510`.

Run order:

```bash
uv run tradegy backtest mes_post_high_event_first_move_fade_long
uv run tradegy backtest mes_post_high_event_first_move_fade_short
uv run tradegy walk-forward mes_post_high_event_first_move_fade_long --holdout-months 6
uv run tradegy walk-forward mes_post_high_event_first_move_fade_short --holdout-months 6
```

## Fourth Batch Results

Single-window sanity and rolling walk-forward backtests were run on 2026-05-11 with the standard futures cost model.

Single-window sanity:

| Spec | Trades | PnL | Per-trade Sharpe | Result |
|---|---:|---:|---:|---|
| `mes_post_high_event_first_move_fade_long` | 58 | +$40.12 | +0.030 | Passes sanity. |
| `mes_post_high_event_first_move_fade_short` | 83 | +$168.38 | +0.070 | Passes sanity. |

Rolling walk-forward (3.0y train / 1.0y test / 1.0y step, 3 windows):

| Spec | Avg IS Sharpe | Avg OOS Sharpe | Worst OOS | Avg IS Trades | Avg OOS Trades | Gate |
|---|---:|---:|---:|---:|---:|---|
| `mes_post_high_event_first_move_fade_long` | +0.063 | -0.052 | -0.167 | 27.3 | 12.7 | FAIL — OOS/IS ratio -0.82, far below 0.5 |
| `mes_post_high_event_first_move_fade_short` | -0.028 | +0.052 | -0.413 | 39.0 | 12.0 | FAIL — IS Sharpe not positive |

Per-window detail (long fade):

| Train | Test | IS Sharpe | OOS Sharpe | IS Trades | OOS Trades |
|---|---|---:|---:|---:|---:|
| 2019-05 → 2022-05 | 2022-05 → 2023-05 | +0.141 | -0.052 | 16 | 17 |
| 2020-05 → 2023-05 | 2023-05 → 2024-05 | +0.047 | +0.064 | 29 | 12 |
| 2021-05 → 2024-05 | 2024-05 → 2025-05 | +0.001 | -0.167 | 37 | 9 |

Per-window detail (short fade):

| Train | Test | IS Sharpe | OOS Sharpe | IS Trades | OOS Trades |
|---|---|---:|---:|---:|---:|
| 2019-05 → 2022-05 | 2022-05 → 2023-05 | -0.007 | +0.154 | 38 | 12 |
| 2020-05 → 2023-05 | 2023-05 → 2024-05 | +0.026 | -0.413 | 42 | 12 |
| 2021-05 → 2024-05 | 2024-05 → 2025-05 | -0.102 | +0.416 | 37 | 12 |

Evidence packets:

| Spec | Backtest | Walk-forward |
|---|---|---|
| `mes_post_high_event_first_move_fade_long` | `data/evidence/mes_post_high_event_first_move_fade_long__backtest__20260511T054606.json` | `data/evidence/mes_post_high_event_first_move_fade_long__walk_forward__20260511T054633.json` |
| `mes_post_high_event_first_move_fade_short` | `data/evidence/mes_post_high_event_first_move_fade_short__backtest__20260511T054606.json` | `data/evidence/mes_post_high_event_first_move_fade_short__walk_forward__20260511T054634.json` |

Interpretation: the broader high-importance event pool produced enough trades to clear the sanity bar (58 long, 83 short) and the full-sample per-trade Sharpe was positive in both directions, but rolling walk-forward refuses both specs cleanly. For the long-fade, IS Sharpe was positive in all 3 rolling 3y training windows but OOS reverted in 2 of 3 — the mechanism degrades out-of-sample. For the short-fade, NONE of the 3 rolling IS windows produced positive Sharpe; the full-sample positive came entirely from a single OOS window (2019-2022 OOS +0.154), making it a regime artifact rather than a stable edge.

The disciplined conclusion: post-event first-move fade does NOT generalize from CPI/FOMC to the canonical high-importance pool. The batch-2 directional positives on CPI/FOMC were either regime-specific or sample-size noise that broader pooling exposes. No parameter expansion is justified — sample dilution (long-fade IS trade count 27.3, OOS 12.7) is not a parameter problem but an event-frequency problem, and tuning thresholds to recover trade count would be post-hoc on a now-known-failed mechanism.

This batch closes the "broader event pool" line. The next concrete cross-domain inputs to attempt, in priority order, are gamma exposure (SPX/SPY chain-derived; zero data spend for an EOD pilot) and breadth (no admitted source yet; ~$3 for a short-history pilot). Looser stops, looser thresholds, or per-event-type pool subselection are all forbidden.

## First Batch Results

Single-window sanity backtests were run immediately after pre-registration on 2026-05-10 with the standard futures cost model: `tick_size=0.25`, `slippage_ticks=0.5/side`, `commission_round_trip=$1.50`.

| Spec | Trades | PnL | Per-trade Sharpe | Result |
|---|---:|---:|---:|---|
| `mes_vix_confirmed_or30_breakout` | 401 | -$960.02 | -0.261 | Fails sanity. |
| `mes_vix_falling_gap_fade` | 366 | -$482.11 | -0.318 | Fails sanity. |
| `mes_mnq_divergence_fade_long` | 4,934 | -$7,943.62 | -0.215 | Fails sanity. |
| `mes_mnq_divergence_fade_short` | 4,961 | -$7,981.62 | -0.206 | Fails sanity. |

Evidence packets:

| Spec | Evidence packet |
|---|---|
| `mes_vix_confirmed_or30_breakout` | `data/evidence/mes_vix_confirmed_or30_breakout__backtest__20260510T062510.json` |
| `mes_vix_falling_gap_fade` | `data/evidence/mes_vix_falling_gap_fade__backtest__20260510T062509.json` |
| `mes_mnq_divergence_fade_long` | `data/evidence/mes_mnq_divergence_fade_long__backtest__20260510T062507.json` |
| `mes_mnq_divergence_fade_short` | `data/evidence/mes_mnq_divergence_fade_short__backtest__20260510T062507.json` |

Interpretation: daily VIX plus simple cross-index z-score gates are not selective enough. The next MES intraday work should not tune these parameters. It should add the missing independent inputs from the deferred batch, starting with event first-move and gamma-surface features.

## Deferred Batch

These ideas are not being implemented as dummy substitutes. They require the missing inputs above.

| Idea | Blocker | Next concrete task |
|---|---|---|
| `post_cpi_first_move_fade` | Unblocked 2026-05-10. | First executable CPI/FOMC specs pre-registered in the second batch. |
| `rates_confirmed_macro_continuation` | Unblocked 2026-05-10. | Use admitted `zn_1m_ohlcv` and `zt_1m_ohlcv`; DXY remains optional. |
| `breadth_confirmed_vwap_reversion` | No breadth source admitted. | Admit breadth source, then gate VWAP reversion on participation divergence. |
| `gamma_strike_pin_fade` | No gamma exposure feature. | Build SPX/SPY chain-derived gamma features from point-in-time chains. |
| `eod_breadth_continuation` | No breadth/day-trend gate. | Add day-trend and breadth features before testing. |

## Data Acquisition Matrix

Metadata/cost probes run 2026-05-10 against the current Databento key. ZN and ZT were subsequently downloaded with explicit confirmation.

| Need | Source | Probe result | Recommendation |
|---|---|---:|---|
| Treasury/rates confirmation | Databento `GLBX.MDP3`, `ZN.FUT` 1m, 2019-05-06 to 2026-04-30 | $11.11 | Downloaded, ingested as `zn_1m_ohlcv`, audit passed. |
| Treasury/rates confirmation | Databento `GLBX.MDP3`, `ZT.FUT` 1m, same window | $8.81 | Downloaded, ingested as `zt_1m_ohlcv`, audit passed. |
| Treasury/rates confirmation | Databento `GLBX.MDP3`, `ZF.FUT` 1m, same window | $10.21 | Pull if building curve-slope features. |
| Treasury/rates confirmation | Databento `GLBX.MDP3`, `ZB.FUT` 1m, same window | $9.82 | Optional; long-bond proxy. |
| Short-rate futures | Databento `GLBX.MDP3`, `SR3.FUT` 1m, same window | $108.98 | Defer unless SR3-specific mechanism is registered. |
| Intraday VX | Databento `XCBF.PITCH`, `VX.FUT` 1m, 2019-05-06 to 2026-04-30 | $1,849.93 | Too expensive for a first pass; use daily VIX or pull a narrow event window only. |
| Intraday VX trades | Databento `XCBF.PITCH`, `VX.FUT` trades, same window | $10,456.88 | Reject for now. |
| Intraday VX top-of-book | Databento `XCBF.PITCH`, `VX.FUT` `mbp-1`, same window | $30,077.70 | Reject for now. |
| Intraday VX pilot | Sierra Chart Denali / Historical Data Service, CFE VX SCID files | Existing Sierra package plus CFE exchange fee: $10/month top-of-book or $12/month depth if real-time is needed | Shipped as `vx_1m_ohlcv`; SCID files are parsed directly, aggregated to 1m, and stitched to front-month. |
| SPX options gamma | Databento `OPRA.PILLAR`, `SPX.OPT`, 2020-2024 definitions | $13.61 | Pull with `statistics` and `cbbo-1m` if gamma work is approved. |
| SPX options gamma | Databento `OPRA.PILLAR`, `SPX.OPT`, 2020-2024 statistics | $39.09 | Likely needed for OI/stat fields; verify schema before download. |
| SPX options gamma | Databento `OPRA.PILLAR`, `SPX.OPT`, 2020-2024 `cbbo-1m` | $486.71 | Feasible for serious gamma-surface research. |
| SPX options gamma | Databento `OPRA.PILLAR`, `SPX.OPT`, 2023-2025 `cbbo-1m` | $243.28 | Better first gamma pull if limiting spend. |
| SPY options gamma | Databento `OPRA.PILLAR`, `SPY.OPT`, 2020-2024 `cbbo-1m` | $684.95 | More expensive than SPX but still feasible. |
| SPY options OHLCV | Databento `OPRA.PILLAR`, `SPY.OPT`, 2020-2024 `ohlcv-1m` | $5,132.89 | Reject; use `cbbo-1m`/definitions/statistics instead. |
| Sector ETF breadth proxy | Databento `DBEQ.BASIC`, 11 sector ETFs, 2024 `ohlcv-1m` | $2.98 | Cheap but short history; useful for pilot only. |
| Sector ETF breadth proxy | Databento `EQUS.MINI`, 11 sector ETFs, 2024 `ohlcv-1m` | $0.66 | Cheapest 2024-only breadth proxy. |

### Sierra Chart VX Decision

Sierra Chart is the admitted VX path over full-history Databento VX for this research lane.

Evidence from Sierra documentation checked 2026-05-10:

| Question | Finding | Research implication |
|---|---|---|
| Does Sierra cover CFE? | Denali lists CFE in the supported exchange set, and the Historical Data Service includes CBOE Futures Exchange (CFE). | VX futures are plausible through Sierra rather than Databento `XCBF.PITCH`. |
| Is intraday history available? | Denali and the Historical Data Service both document historical intraday data. For non-CME/EUREX futures and cash-index symbols, 1-minute history is generally at least back to 2010 if the symbol traded then, with symbol-dependent depth. | The 2019-2026 VX research window should be feasible, but this must be verified by downloading/exporting one continuous VX chart. |
| What is the expected marginal cost? | CFE Top Of Book is documented at $10/month; CFE Market Depth is $12/month. Historical-only access may be available through the included Historical Data Service, but real-time/non-delayed CFE needs the exchange activation. | Cost is low enough for a pilot and far below the Databento full-history VX estimate. |
| Can it export data usable by this repo? | `Edit >> Export Bar Data to Text File` exports loaded chart bars with `Date, Time, Open, High, Low, Last, Volume, NumberOfTrades, BidVolume, AskVolume`; this matches `src/tradegy/ingest/csv_sierra.py`. | Existing Sierra CSV ingest can be reused; pass `input_tz="UTC"` if the export is from `Export and Edit Intraday Data`, or set the chart timezone to UTC before `Export Bar Data to Text File`. |
| How should continuous VX be exported? | `Export and Edit Intraday Data` exports the underlying current symbol file only for continuous futures charts. `Export Bar Data to Text File` exports the loaded chart bars, and Sierra's docs explicitly direct enabling Continuous Futures Contract for larger futures history exports. | Use chart-level bar export, not raw intraday-file export, for continuous VX research data. |
| Is this admitted data yet? | Yes. Sierra downloaded all monthly VX contracts needed for 2019-05 through 2026-04, the repo now has `sierra_chart_scid_vx` ingest plus `vx_1m_ohlcv`, and the full source audit passed. | VX-derived features can now be pre-registered for the next batch. |

Automation findings:

| Path | Scriptability | Decision |
|---|---|---|
| Direct `.scid` parsing | Scriptable because Sierra documents the binary intraday file header and 40-byte records. | Chosen path. `src/tradegy/ingest/sierra_scid.py` parses VX contract files directly, aggregates tick/bar records to 1m, and performs deterministic front-month stitching. |
| Manual chart-bar export | Low automation, high confidence. `Edit >> Export Bar Data to Text File` exports exactly the loaded continuous chart bars. | No longer required for first ingest; keep as an external cross-check if SCID stitch quality is questioned. |
| `Write Bar Data to File` / `Write Bar and Study Data To File` Sierra studies | Semi-automated. Once attached to a configured continuous chart, Sierra continuously writes the loaded chart data to a text file. `Write Bar Data to File` explicitly writes one output file for continuous futures charts. | Optional live/update path later; not needed for historical backfill. |
| DTC historical data server | Programmatic socket API. Sierra documents a historical data port and one historical request per connection, but its Restrictions section says real-time or historical data from CME Group, EUREX, NASDAQ, CBOE, and US equities cannot be accessed through the DTC server. VX is CFE/Cboe-family data, so assume rejection until tested locally. | Do not build a DTC integration as the first VX path. Test only after manual/chart-file export succeeds. |
| GUI automation | Technically possible but brittle. | Reject unless no supported export/file-writing path works. |

Validation workflow before research use:

1. Confirm Sierra Chart has the needed monthly VX files under its Data folder. The 2026-05-10 check found all 84 monthly contracts from 2019-05 through 2026-04 present.
2. Ingest with `uv run tradegy ingest <SierraChart Data folder> --source-id vx_1m_ohlcv`.
3. Run `uv run tradegy audit vx_1m_ohlcv` and inspect gap findings before feature work.
4. Build VX features only after the source audit is clean enough for the specific signal cadence.

Actual 2026-05-10 ingest result:

| Metric | Value |
|---|---:|
| Raw SCID records parsed inside coverage window | 86,257,359 |
| Continuous 1m rows | 1,674,211 |
| Overlapping contract-minutes dropped | 921,153 |
| Partitions written | 2,175 |
| Coverage start | 2019-05-06 00:00:00 UTC |
| Coverage end | 2026-04-30 23:59:00 UTC |
| Batch id | `1c37c5474c17178f` |
| Audit | Pass: no findings |

Decision: use Sierra Chart SCID as the low-cost VX backfill path. Do not purchase full-history Databento VX unless the direct SCID source audit reveals unfixable quality defects and a VX-specific hypothesis still justifies the spend.

### Rates Acquisition Result

ZN and ZT are admitted as the first rates-confirmation inputs.

| Source | Download cost | Download rows | Ingested continuous rows | Batch id | Audit |
|---|---:|---:|---:|---|---|
| `zn_1m_ohlcv` | $11.11 | 3,042,443 | 2,336,096 | `bda6c900fa5bebb4` | Pass: no findings |
| `zt_1m_ohlcv` | $8.81 | 2,413,897 | 1,957,772 | `c67aea1d74c3b664` | Pass: no findings |

Databento emitted reduced-quality warnings for some historical dates, including 2020-02-27, 2020-02-28, and 2020-06-30. The first ingest attempt also exposed a root data-shape issue: parent-symbol pulls include exchange-listed calendar spreads (`ZNM9-ZNU9`, `ZTM9-ZTU9`, etc.) whose negative spread prices are valid spread data but invalid for an outright front-month futures series. The Databento OHLCV ingest now excludes symbols containing `-` before front-month selection; after reingest, both ZN and ZT audits passed.

Acquisition order if continuing MES intraday research:

1. Implement VX + rates-confirmed event continuation/fade features using `vx_1m_ohlcv`, `zn_1m_ohlcv`, and `zt_1m_ohlcv`.
2. If willing to spend ~$250-$550, pull SPX OPRA definitions/statistics/`cbbo-1m` for 2023-2025 first, then 2020-2024 only if the feature pipeline looks sound.
3. Use equity breadth proxies only as a pilot; they do not solve the 2019-2026 walk-forward window unless a longer equities source is admitted.
4. Do not buy full-history VX from Databento unless Sierra cannot provide a complete VX window and a VX-specific hypothesis still justifies the spend.

## Gate Discipline

The same anti-overfitting gates apply unless a hypothesis YAML explicitly tightens them before first test:

| Gate | Requirement |
|---|---|
| Sanity | At least 30 trades and in-sample per-trade Sharpe > 0. |
| Walk-forward | Average OOS Sharpe >= 50% of average IS Sharpe and average IS Sharpe > 0. |
| CPCV | Median Sharpe > 0.8 and negative paths < 20%. |
| Holdout | Holdout Sharpe >= 50% of walk-forward Sharpe. |

No parameter expansion is allowed after seeing results. Failed variants must be killed with trade count, IS/OOS Sharpe, and reason.

## Original Run Order

Run single-window sanity first, then walk-forward only for specs with nonzero, positive sanity evidence. The 2026-05-10 run found no positive sanity evidence, so walk-forward was not run.

```bash
uv run tradegy backtest mes_vix_confirmed_or30_breakout
uv run tradegy backtest mes_vix_falling_gap_fade
uv run tradegy backtest mes_mnq_divergence_fade_long
uv run tradegy backtest mes_mnq_divergence_fade_short

uv run tradegy walk-forward mes_vix_confirmed_or30_breakout --holdout-months 6
uv run tradegy walk-forward mes_vix_falling_gap_fade --holdout-months 6
uv run tradegy walk-forward mes_mnq_divergence_fade_long --holdout-months 6
uv run tradegy walk-forward mes_mnq_divergence_fade_short --holdout-months 6
```

If the first batch fails, the conclusion is not "try looser stops." The correct next work is admitting the deferred independent inputs, especially event first-move, breadth, rates, and gamma-surface features.
