# MES Intraday Directional Research Plan

**Status:** First executable batch killed at sanity, 2026-05-10  
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
| Event first-move feature | Post-CPI/FOMC first-move fade needs the sign and magnitude of the immediate reaction. | Feature or strategy class that records event-time reference price and first reaction window. |
| Breadth / ADD / TICK / sector breadth | EOD continuation needs market-wide participation, not just index price. | Admit a breadth source with live/historical parity. |
| SPX/SPY gamma surface | 0DTE gamma pin / flip-zone research. | Build point-in-time chain-derived gamma exposure features. |
| Rates / DXY | Macro continuation confirmation. | Admit TY/ZN/2Y proxy and DXY or liquid futures proxy. |

## First Executable Batch

The first batch intentionally uses only existing registered features and strategy classes. It is not tuned from results; parameters below are pre-registered.

| Spec | Mechanism | Independent gates | Status |
|---|---|---|---|
| `mes_vix_confirmed_or30_breakout` | OR30 continuation when high/expanding VIX suggests real directional repricing. | OR30 break with volume, VIX percentile > 0.50, VIX 5d change > 0, event quiet, early/mid RTH window. | Killed at sanity. |
| `mes_vix_falling_gap_fade` | Overnight gap mean reversion when vol is compressing and no macro catalyst is active. | Gap threshold, VIX percentile < 0.50, VIX 5d change < 0, event quiet, early RTH window. | Killed at sanity. |
| `mes_mnq_divergence_fade_long` | MES cheap vs MNQ mean reverts as index-arb pressure closes the spread. | MES/MNQ z < -2, RTH time gate, event quiet. | Killed at sanity. |
| `mes_mnq_divergence_fade_short` | MES rich vs MNQ mean reverts as index-arb pressure closes the spread. | MES/MNQ z > +2, RTH time gate, event quiet. | Killed at sanity. |

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
| `post_cpi_first_move_fade` | No event first-move sign/magnitude feature. | Add event-reaction feature or class, then pre-register long/short reversal specs. |
| `rates_confirmed_macro_continuation` | No rates/DXY source admitted. | Admit rates/DXY inputs with parity and backfill. |
| `breadth_confirmed_vwap_reversion` | No breadth source admitted. | Admit breadth source, then gate VWAP reversion on participation divergence. |
| `gamma_strike_pin_fade` | No gamma exposure feature. | Build SPX/SPY chain-derived gamma features from point-in-time chains. |
| `eod_breadth_continuation` | No breadth/day-trend gate. | Add day-trend and breadth features before testing. |

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
