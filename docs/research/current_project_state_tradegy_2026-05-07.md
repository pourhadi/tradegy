# Research: Current project state — tradegy

## Question
Bring the assistant up to speed on the current state of `/Users/dan/code/tradegy` using read-only project inspection.

## Summary
`tradegy` is a Python 3.12+ quant research/trading platform with shipped infrastructure for futures research, feature engineering, backtesting, hypothesis auto-generation, execution, monitoring, and a large options-vol-selling workstream. The original MES directional-alpha search has not produced a validated survivor; the validated/current edge is options vol-selling, especially SPY `PCS+IC+JL+IV<0.25`, plus a newer MES 0DTE paper/live daemon using a daily-fire iron-condor config.

No files were edited during this research.

## Findings

### Repository / branch state
- Current branch: `main`.
- `main` is clean but **47 commits ahead of `origin/main`**.
- Latest local commit: `9fba61e options: extract close-cost helper + regression-test the intrinsic fallback`.
- There are substantial ignored runtime artifacts under `data/` (~12G) and `.venv/`.
- Operational caution: launchd jobs are loaded for:
  - `com.tradegy.mes-0dte-entry`
  - `com.tradegy.mes-0dte-manage`
  - `com.tradegy.live-options`
- Recent local live logs reportedly show 2026-05-07 operational issues: IB Gateway/TWS unreachable at `127.0.0.1:4002` and some shell paths using `python` where only `uv`/`python3` may be available.

### Stack
- Python `>=3.12`, managed with `uv`.
- Main dependencies: `polars`, `pydantic v2`, `pyyaml`, `typer`, `rich`, `ib-async`, `exchange-calendars`, `anthropic`, `streamlit`, `altair`, `scikit-learn`.
- CLI entrypoint: `tradegy = tradegy.cli:app`.
- Pytest default excludes `slow`: `-q -m 'not slow'`.

### Codebase shape
Major source areas:
- `src/tradegy/features/` — feature transforms and engine.
- `src/tradegy/harness/` — futures backtest/walk-forward/CPCV.
- `src/tradegy/strategies/` — futures strategy/auxiliary classes.
- `src/tradegy/options/` — options chain, Greeks, strategies, runners, walk-forward/CPCV, MES 0DTE.
- `src/tradegy/execution/` — FSM, idempotency, risk caps, kill-switch, IBKR routers, divergence, reconciliation.
- `src/tradegy/live/` — live IBKR/options orchestration, close loop, registry, doctor.
- `src/tradegy/monitoring/` — health checks and alert routing.
- `src/tradegy/auto_generation/` — Anthropic hypothesis/variant generation, feature stats, kill-log injection, market scan, auto-test.
- `src/tradegy/regime/` — session labeling/classifier work.

### CLI surface
The CLI includes roughly 30 commands across data/features, registry lookup, futures validation, auto-generation, options research, live options, and regime labeling/classification. Important commands include `backtest`, `walk-forward`, `cpcv`, `hypothesize`, `auto-vary`, `auto-test`, `options-walk-forward`, `options-cpcv`, `live-options`, and `train-regime-classifier`.

### Tests
Static inventory found:
- 83 test files.
- About 814 static test functions / ~815 collected items.
- Default fast set is roughly 642 items before runtime skips.
- Slow/integration tests are auto-marked for real options-chain fixtures, including SPX/SPY/MES options chain fixtures.

### Data / registries
Registered inventory:
- 20 data sources.
- 59 features, all currently `lifecycle_state: in_development`.
- 6 hypotheses: 2 promoted, 1 proposed, 3 killed.
- 76 strategy YAML specs, all `metadata.status: draft`, `operational.tier: proposal_only`, `operational.enabled: true`.

Key data-source families:
- Futures/equity bars: MES/MNQ/M2K/YM, ES 1s, MES 5s, SPY 1m.
- Options chains: SPX/SPY/XSP/QQQ/IWM/EEM/GLD/DIA/TLT/XLE ORATS chains, plus Databento `mes_options_chain`.
- Regime/event inputs: `vix_daily`, `econ_events`.

Runtime artifact layout includes `data/raw/`, `data/features/`, `data/evidence/`, `data/auto_generation/`, `data/feature_stats/`, `data/live_options/`, and `data/session_labels/`.

### Futures research status
- Single-instrument MES directional strategy search has repeatedly failed.
- Round 1–3 variants were killed at sanity; selectivity is the binding constraint, not stop sizing.
- Round 4 introduced cross-domain inputs (VIX, macro events), and some related data/features/specs now exist locally, but the main validated edge has shifted toward options.

### Auto-generation status
Shipped:
- Hypothesis schema/loader.
- Anthropic hypothesis and variant generators.
- Auto-test orchestrator.
- Feature distribution-stat prompt injection.
- Kill-record prompt injection.
- Market-scan prompt injection.
- Holdout support in auto-test.

Pending:
- Embedding-based diversity check.
- Deflated Sharpe Ratio.
- Five-test triage scorer integration.

### Execution / monitoring status
Execution shipped:
- Order FSM, idempotency keys, append-only transition log, risk caps, kill-switch, session-flatten planner, IBKR router/status mapping, divergence detector, reconciliation loop, and IBKR options combo routing.

Monitoring shipped:
- Check framework and runner, alert router, broker connectivity, data freshness, time skew, and process liveness.

Pending/deferred:
- Feature compute lag/drift checks, model freshness, margin headroom, and selection-layer/LLM health.

### Options vol-selling status
Shipped:
- ORATS ingest/chain reader.
- Databento MES options ingest/chain reader.
- Black-Scholes Greeks/IV solver.
- Chain features, multi-leg position/cost/risk model, option strategy ABC and runner, portfolio runner, multi-source runner, options walk-forward/CPCV, and the major option strategy catalog.

Important findings:
- Bare SPX vol-selling strategies failed rolling anti-overfit gates.
- Low-IV-gated wrappers survived; canonical `IV>0.50` failed.
- Defensible retail config is SPY `PCS+IC+JL+IV<0.25`.
- Documented $5K SPY config: 16-year walk-forward OOS Sharpe around `+0.867`, about `13.5%` annualized return on capital, low trade count.
- EEM looked strong over 2020+ but failed 16-year diligence and is no longer the recommended $5K path.

### MES 0DTE status
- MES futures-options data path is resolved via Databento `GLBX.MDP3`, including X-prefix daily expirations.
- Data covers roughly 2023-02-13 through 2025-04-30 for MES options chain source.
- Infrastructure shipped: `Mes0dteIronCondor`, `Mes0dtePcs`, `VixGatedStrategy`, `zero_dte_runner`, `scripts/live_mes_0dte.py`, launchd plists/wrappers, and dashboard script.
- Current documented daemon config is daily-fire:
  - Iron condor, $10 short offsets / $10 wings.
  - 75% profit take.
  - No VIX gate.
  - 10:00 ET entry.
  - 1 contract.

### Live scripts
- SPY EOD options: `scripts/live_options_daily.sh` and `scripts/com.tradegy.live-options.plist`, invoking `tradegy live-options`.
- MES 0DTE: `scripts/live_mes_0dte.py`, `live_mes_0dte_entry.sh`, `live_mes_0dte_manage.sh`, and corresponding launchd plists.
- Utilities/dashboards: `mes_0dte_dashboard.py`, `ingest_mes_options_full_grid.py`, `management_sweep.py`, `multi_source_5k.py`, `predict_all_sessions.py`, `run_pairs_backtest.py`.

### Documentation discrepancies
Several docs are stale or mixed-era:
- `trading_platform_docs/README.md` still says ES-only, all docs are drafts, and omits docs 14/15/runbook.
- `CLAUDE.md` header says last pinned 2026-05-02 but includes 2026-05-06 updates; test counts conflict.
- `trading_platform_docs/03_strategy_class_registry.md` lags implemented strategy/auxiliary classes.
- `trading_platform_docs/11_execution_layer_spec.md` header says Phase 1, while table/body say Phase 1+2+3A+3B+3C shipped.
- `trading_platform_docs/14_options_volatility_selling.md` contains older phase-plan text that conflicts with later shipped sections.
- `trading_platform_docs/15_5k_options_capital_plan.md` has internal conflicts between early “not validated / needs data” language and later “data resolved / OOS passed / daemon switched” updates.
- `trading_platform_docs/15_live_options_runbook.md` mixes obsolete EEM guidance with current SPY guidance and does not cleanly separate SPY EOD from MES 0DTE operation.
- MES daemon script/plist comments may mention older VIX-gated/PT50/10:25 timing while constants now reflect daily-fire/PT75/no-gate behavior.

## Sources
Project files inspected only; no external web sources were used.

Key project sources:
- `CLAUDE.md`
- `pyproject.toml`
- `src/tradegy/cli.py`
- `src/tradegy/**`
- `tests/**`
- `registries/data_sources/*.yaml`
- `registries/features/*.yaml`
- `hypotheses/*.yaml`
- `strategies/*.yaml`
- `scripts/*`
- `trading_platform_docs/README.md`
- `trading_platform_docs/06_hypothesis_system.md`
- `trading_platform_docs/07_auto_generation.md`
- `trading_platform_docs/11_execution_layer_spec.md`
- `trading_platform_docs/12_live_monitoring_spec.md`
- `trading_platform_docs/14_options_volatility_selling.md`
- `trading_platform_docs/15_5k_options_capital_plan.md`
- `trading_platform_docs/15_live_options_runbook.md`

## Options
1. Continue with implementation work using the current architecture and treat docs discrepancies as known debt.
2. Do a documentation cleanup/pinning pass first, especially README/doc 03/doc 14/doc 15/live runbooks.
3. Investigate live operational state next: launchd status, IBKR connectivity, live logs, kill-switch state, and whether paper jobs fired correctly.
4. Push/PR the 47 unpushed local commits before destructive git operations.

## Recommendation
For future work, assume the live/current project center is:
1. SPY EOD options vol-selling as the validated retail-capital path.
2. MES 0DTE daily-fire paper daemon as the newest active operations path.
3. Futures directional MES research remains mostly killed/experimental unless cross-domain Round 4 is resumed.

Before any live-operation changes, inspect runtime logs and IBKR connectivity. Before any architectural/code changes, update the relevant docs in the same iteration, per project discipline.

## Unknowns / Follow-ups
- Whether IB Gateway/TWS is currently running and accepting connections at `127.0.0.1:4002`.
- Whether launchd jobs are expected to be active right now or are remnants from testing.
- Whether paper/live fills have matched model assumptions.
- Whether the 47 ahead commits should be pushed immediately.
- Exact pytest collected count if dynamically collected with current runtime environment.
