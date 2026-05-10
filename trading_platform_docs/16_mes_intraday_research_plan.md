# MES Intraday Directional Research Plan

**Status:** First executable batches killed at sanity; Sierra Chart VX path scoped, 2026-05-10  
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
| MNQ/M2K/YM 1m bars | `mnq_1m_ohlcv`, `m2k_1m_ohlcv`, `ym_1m_ohlcv` | Cross-index divergence inputs. |
| VIX daily | `vix_daily_close`, `vix_daily_pctile_252`, `vix_daily_5d_change` | Regime level and vol-compression/expansion direction. |
| Macro events | `econ_events`, hours-to-next-event features | Supports pre-event drift and quiet-window gates. |
| OR30 / VWAP / prior close | `mes_or30_high`, `mes_or30_low`, `mes_vwap`, `mes_prior_rth_close` | Existing intraday anchors. |

Not yet available and therefore not used in the executable first batch:

| Needed input | Why it matters | Required before testing |
|---|---|---|
| Intraday VIX/VX | True intraday vol confirmation and VIX divergence. | Admit an intraday VX/VIX source or explicitly decide daily VIX is sufficient for the research question. |
| Breadth / ADD / TICK / sector breadth | EOD continuation needs market-wide participation, not just index price. | Admit a breadth source with live/historical parity. |
| SPX/SPY gamma surface | 0DTE gamma pin / flip-zone research. | Build point-in-time chain-derived gamma exposure features. |
| Rates / DXY | Macro continuation confirmation. | Admit TY/ZN/2Y proxy and DXY or liquid futures proxy. |

Newly added from existing data on 2026-05-10:

| Feature | Raw data needed? | Notes |
|---|---|---|
| `mes_hours_since_last_cpi` | No | Built from `econ_events` + `mes_1m_bars`; no-lookahead/reproducibility passed 50/50. |
| `mes_cpi_30m_reaction_return` | No | First 30m signed MES return after CPI; emits only after the 30m reaction window completes. |
| `mes_fomc_30m_reaction_return` | No | First 30m signed MES return after FOMC statements; emits only after the 30m reaction window completes. |

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
| `rates_confirmed_macro_continuation` | No rates/DXY source admitted. | Admit rates/DXY inputs with parity and backfill. |
| `breadth_confirmed_vwap_reversion` | No breadth source admitted. | Admit breadth source, then gate VWAP reversion on participation divergence. |
| `gamma_strike_pin_fade` | No gamma exposure feature. | Build SPX/SPY chain-derived gamma features from point-in-time chains. |
| `eod_breadth_continuation` | No breadth/day-trend gate. | Add day-trend and breadth features before testing. |

## Data Acquisition Matrix

Metadata/cost probes run 2026-05-10 against the current Databento key. No downloads were performed.

| Need | Source | Probe result | Recommendation |
|---|---|---:|---|
| Treasury/rates confirmation | Databento `GLBX.MDP3`, `ZN.FUT` 1m, 2019-05-06 to 2026-04-30 | $11.11 | Pull when ready; cheap and directly useful. |
| Treasury/rates confirmation | Databento `GLBX.MDP3`, `ZT.FUT` 1m, same window | $8.81 | Pull with `ZN`; captures 2Y-rate sensitivity. |
| Treasury/rates confirmation | Databento `GLBX.MDP3`, `ZF.FUT` 1m, same window | $10.21 | Pull if building curve-slope features. |
| Treasury/rates confirmation | Databento `GLBX.MDP3`, `ZB.FUT` 1m, same window | $9.82 | Optional; long-bond proxy. |
| Short-rate futures | Databento `GLBX.MDP3`, `SR3.FUT` 1m, same window | $108.98 | Defer unless SR3-specific mechanism is registered. |
| Intraday VX | Databento `XCBF.PITCH`, `VX.FUT` 1m, 2019-05-06 to 2026-04-30 | $1,849.93 | Too expensive for a first pass; use daily VIX or pull a narrow event window only. |
| Intraday VX trades | Databento `XCBF.PITCH`, `VX.FUT` trades, same window | $10,456.88 | Reject for now. |
| Intraday VX top-of-book | Databento `XCBF.PITCH`, `VX.FUT` `mbp-1`, same window | $30,077.70 | Reject for now. |
| Intraday VX pilot | Sierra Chart Denali / Historical Data Service, CFE VX chart export | Existing Sierra package plus CFE exchange fee: $10/month top-of-book or $12/month depth if real-time is needed | Preferred low-cost VX pilot path; validate one exported VX continuous 1m file before admitting as a registry source. |
| SPX options gamma | Databento `OPRA.PILLAR`, `SPX.OPT`, 2020-2024 definitions | $13.61 | Pull with `statistics` and `cbbo-1m` if gamma work is approved. |
| SPX options gamma | Databento `OPRA.PILLAR`, `SPX.OPT`, 2020-2024 statistics | $39.09 | Likely needed for OI/stat fields; verify schema before download. |
| SPX options gamma | Databento `OPRA.PILLAR`, `SPX.OPT`, 2020-2024 `cbbo-1m` | $486.71 | Feasible for serious gamma-surface research. |
| SPX options gamma | Databento `OPRA.PILLAR`, `SPX.OPT`, 2023-2025 `cbbo-1m` | $243.28 | Better first gamma pull if limiting spend. |
| SPY options gamma | Databento `OPRA.PILLAR`, `SPY.OPT`, 2020-2024 `cbbo-1m` | $684.95 | More expensive than SPX but still feasible. |
| SPY options OHLCV | Databento `OPRA.PILLAR`, `SPY.OPT`, 2020-2024 `ohlcv-1m` | $5,132.89 | Reject; use `cbbo-1m`/definitions/statistics instead. |
| Sector ETF breadth proxy | Databento `DBEQ.BASIC`, 11 sector ETFs, 2024 `ohlcv-1m` | $2.98 | Cheap but short history; useful for pilot only. |
| Sector ETF breadth proxy | Databento `EQUS.MINI`, 11 sector ETFs, 2024 `ohlcv-1m` | $0.66 | Cheapest 2024-only breadth proxy. |

### Sierra Chart VX Decision

Sierra Chart is the preferred next VX path over full-history Databento VX for this research lane.

Evidence from Sierra documentation checked 2026-05-10:

| Question | Finding | Research implication |
|---|---|---|
| Does Sierra cover CFE? | Denali lists CFE in the supported exchange set, and the Historical Data Service includes CBOE Futures Exchange (CFE). | VX futures are plausible through Sierra rather than Databento `XCBF.PITCH`. |
| Is intraday history available? | Denali and the Historical Data Service both document historical intraday data. For non-CME/EUREX futures and cash-index symbols, 1-minute history is generally at least back to 2010 if the symbol traded then, with symbol-dependent depth. | The 2019-2026 VX research window should be feasible, but this must be verified by downloading/exporting one continuous VX chart. |
| What is the expected marginal cost? | CFE Top Of Book is documented at $10/month; CFE Market Depth is $12/month. Historical-only access may be available through the included Historical Data Service, but real-time/non-delayed CFE needs the exchange activation. | Cost is low enough for a pilot and far below the Databento full-history VX estimate. |
| Can it export data usable by this repo? | `Edit >> Export Bar Data to Text File` exports loaded chart bars with `Date, Time, Open, High, Low, Last, Volume, NumberOfTrades, BidVolume, AskVolume`; this matches `src/tradegy/ingest/csv_sierra.py`. | Existing Sierra CSV ingest can be reused; pass `input_tz="UTC"` if the export is from `Export and Edit Intraday Data`, or set the chart timezone to UTC before `Export Bar Data to Text File`. |
| How should continuous VX be exported? | `Export and Edit Intraday Data` exports the underlying current symbol file only for continuous futures charts. `Export Bar Data to Text File` exports the loaded chart bars, and Sierra's docs explicitly direct enabling Continuous Futures Contract for larger futures history exports. | Use chart-level bar export, not raw intraday-file export, for continuous VX research data. |
| Is this admitted data yet? | No. We have documentation evidence only; no VX file has been exported and ingested. | Do not create VX features/specs until the exported file passes coverage, timestamp, and no-duplicate checks. |

Automation findings:

| Path | Scriptability | Decision |
|---|---|---|
| Manual chart-bar export | Low automation, high confidence. `Edit >> Export Bar Data to Text File` exports exactly the loaded continuous chart bars. | Use first. This validates VX coverage before spending engineering time. |
| `Write Bar Data to File` / `Write Bar and Study Data To File` Sierra studies | Semi-automated. Once attached to a configured continuous chart, Sierra continuously writes the loaded chart data to a text file. `Write Bar Data to File` explicitly writes one output file for continuous futures charts. | Best follow-up if manual export validates coverage. Need one sample file because study headers may differ from manual export headers. |
| DTC historical data server | Programmatic socket API. Sierra documents a historical data port and one historical request per connection, but its Restrictions section says real-time or historical data from CME Group, EUREX, NASDAQ, CBOE, and US equities cannot be accessed through the DTC server. VX is CFE/Cboe-family data, so assume rejection until tested locally. | Do not build a DTC integration as the first VX path. Test only after manual/chart-file export succeeds. |
| Direct `.scid` parsing | Scriptable because Sierra documents the binary intraday file header and 40-byte records. | Not preferred for continuous VX. Sierra stores individual contract files and builds continuous contracts dynamically, so a parser would also need robust rollover stitching. |
| GUI automation | Technically possible but brittle. | Reject unless no supported export/file-writing path works. |

Validation workflow before source admission:

1. Activate or confirm Sierra Chart CFE access, update symbol settings, and open the current VX futures contract from `File >> Find Symbol`.
2. Set the chart to a 1-minute intraday bar period, UTC timezone, and non-back-adjusted continuous contract mode unless the research explicitly needs adjusted prices.
3. Set the load range to 2019-05-06 through 2026-04-30, then force `Edit >> Delete All Data And Download` and select all needed contract months.
4. Enable rollover-date display and inspect the Message Log for missing contract months, bad transitions, or download-limit errors.
5. Export with `Edit >> Export Bar Data to Text File`, not `Export and Edit Intraday Data`.
6. Ingest the exported CSV through `ingest_sierra_csv(..., input_tz="UTC")` into a new `vx_1m_ohlcv` data source only after the file proves full-window coverage.
7. If manual export passes, test Sierra's `Write Bar Data to File` or `Write Bar and Study Data To File` on the same chart and compare row counts/timestamps against the manual export before making acquisition repeatable.

Decision: pursue Sierra Chart VX as the low-cost pilot if intraday VX confirmation becomes the binding blocker. Do not purchase full-history Databento VX before this Sierra validation fails.

Acquisition order if continuing MES intraday research:

1. Pull `ZN.FUT` and `ZT.FUT` 1m from Databento; implement rates-confirmed event continuation/fade features.
2. If VX is required before gamma work, validate Sierra Chart VX export/ingest with one continuous 1m file before any Databento VX spend.
3. If willing to spend ~$250-$550, pull SPX OPRA definitions/statistics/`cbbo-1m` for 2023-2025 first, then 2020-2024 only if the feature pipeline looks sound.
4. Use equity breadth proxies only as a pilot; they do not solve the 2019-2026 walk-forward window unless a longer equities source is admitted.
5. Do not buy full-history VX from Databento unless Sierra cannot provide a complete VX window and a VX-specific hypothesis still justifies the spend.

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
