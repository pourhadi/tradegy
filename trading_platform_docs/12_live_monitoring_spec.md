# Live Monitoring Spec

**Status:** Draft for review
**Purpose:** Define the health checks, SLOs, alerts, and auto-halt
policies that determine whether the runtime system is healthy enough to
trade. Several other docs assume monitoring will exist — this one makes
it concrete.

---

## Why this spec exists

`10_review_gap_analysis.md:43-54` flags missing monitoring as a P0
blocker:

> Multiple docs assume auto-disable, drift detection, and operational
> intervention, but alert thresholds, ownership, and escalation paths
> are undefined.

References that already assume monitoring exists:

- Strategy auto-disable (`00_master_architecture.md:179`).
- Daily/weekly loss caps "enforced by the execution layer regardless of
  what any layer above decides" (`00_master_architecture.md:206`).
- Feature live-vs-historical drift alerts
  (`02_feature_pipeline.md:593-597, 628-639`).
- Stage 9 envelope-breach detection and auto-disable
  (`08_development_pipeline.md:232-248`).
- Selection-layer feature health enum and thesis-drift alerts
  (`09_selection_layer.md:251-256`).
- `LiveAdapter.health()` snapshot contract (`src/tradegy/live/base.py:81-83`).

This spec is the operational glue across those references.

---

## Scope

### In scope

- Health-check inventory and SLOs.
- Alert severity matrix and escalation chain.
- Auto-halt triggers and restart conditions.
- Live-vs-historical drift detection methodology.
- Daily/weekly loss-cap monitoring.
- Operational playbooks for degraded modes.

### Out of scope

- Order lifecycle and reconciliation — owned by
  `11_execution_layer_spec.md`.
- Governance authority for re-enabling halted strategies — owned by
  `13_governance_process.md`.
- Hypothesis-system feedback loops — owned by `06_hypothesis_system.md`.

---

## Health-check inventory

Each check has: a target SLO, a measurement source, and a severity
level when the SLO is breached.

| Check                           | SLO                                | Source                                            | Breach severity |
|---------------------------------|------------------------------------|---------------------------------------------------|-----------------|
| Broker connectivity             | Connected; heartbeat ≤ 2 s old     | `IBKRConnection.health()` at `live/ibkr.py:64-73` | CRITICAL        |
| Live data freshness (5s bars)   | Last bar ≤ 7 s old in RTH          | `LiveAdapter.health()['last_seen']`               | WARNING → CRITICAL after 30 s |
| Live data freshness (1s bars)   | Last bar ≤ 3 s old in RTH          | `LiveAdapter.health()['last_seen']`               | WARNING → CRITICAL after 10 s |
| Feature compute lag             | ≤ declared `availability_latency` × 1.5 | feature engine emit timestamps              | WARNING → CRITICAL after 3× |
| Feature drift vs historical     | Within ±2σ rolling distribution    | drift detector (see below)                        | WARNING; CRITICAL on sustained drift |
| Model freshness                 | Per-model `staleness_threshold`    | model registry metadata                           | WARNING → CRITICAL after 2× |
| Position vs broker              | Exact match                        | reconciliation loop (`11_execution_layer_spec.md`) | CRITICAL        |
| Account margin headroom         | Available funds ≥ 1.5× initial margin per concurrent slot | broker account read | WARNING; CRITICAL on breach |
| Daily loss cap                  | Realized + open ≥ −cap             | execution-layer P&L accrual                       | CRITICAL        |
| Weekly loss cap                 | Rolling 5-session realized ≥ −cap  | execution-layer P&L accrual                       | CRITICAL        |
| Selection-layer cycle health    | Last cycle completed ≤ 2× cadence  | selection-layer audit log                         | WARNING; CRITICAL after 4× |
| LLM API availability            | Successful call within last cycle  | selection-layer telemetry                         | WARNING → degraded mode |
| Registry health enum            | All consumed features `green`      | `02_feature_pipeline.md:593-597`                  | varies — see severity matrix |
| Process liveness                | Heartbeat ≤ 5 s                    | runtime watchdog                                  | CRITICAL        |
| Time skew vs broker             | ≤ 2 s                              | NTP-vs-broker timestamp diff                      | CRITICAL        |

The numeric SLOs above are starting defaults for ES/MES at 5 s/1 s
cadences. They are operator-mutable in the runtime config and logged on
change.

---

## Alert severity matrix

| Severity   | Meaning                                                    | Auto-action                                  | Human action SLA |
|------------|------------------------------------------------------------|----------------------------------------------|------------------|
| `INFO`     | Anomaly worth noticing; no impact on trading               | Logged, no notification                      | None             |
| `WARNING`  | Degraded condition; trading continues with caution flags   | Selection layer informed; downgrade strategies that depend on the affected component to no-new-entry | Acknowledge within 1 hour |
| `CRITICAL` | Trading must halt or be restricted                         | Auto-halt per the matrix below; existing protective stops remain | Acknowledge within 5 minutes; resolve before next session |

### Example triggers

| Severity   | Example                                                                       |
|------------|-------------------------------------------------------------------------------|
| INFO       | Single-bar reordering observed in feature drift detector                      |
| INFO       | Selection-layer cycle ran 1.2× cadence (within tolerance)                     |
| WARNING    | Live 5s bar 12 s stale during RTH                                             |
| WARNING    | Feature `mes_session_vwap` 3σ outside historical distribution for last 30 min |
| WARNING    | LLM call failed once; previous-cycle decision held per `09_selection_layer.md:254` |
| CRITICAL   | Broker disconnected for ≥ 5 s with open position                              |
| CRITICAL   | Local position 0, broker position 1L (from reconciliation)                    |
| CRITICAL   | Daily loss cap breached                                                       |
| CRITICAL   | LLM unavailable for ≥ 3 consecutive cycles                                    |
| CRITICAL   | Time skew > 2 s vs broker                                                     |

---

## Escalation chain

| Severity   | Owner role           | Channel              | Cooldown / suppression                        |
|------------|----------------------|----------------------|-----------------------------------------------|
| INFO       | Logged only          | Audit log            | None                                          |
| WARNING    | On-call operator     | Persistent dashboard, email digest at 1 h | Same alert deduped within 15 min   |
| CRITICAL   | On-call operator     | Push notification + SMS + dashboard | Re-fire every 5 min until acknowledged |

For v1 single-operator deployment, "on-call operator" is the single
human owner of the system. The chain is structured so that adding a
second operator later is a config change, not a redesign.

---

## Auto-halt triggers

Auto-halt produces one of three actions, in increasing severity:

1. **No-new-entry.** New entry orders rejected; protective stops and
   exits continue normally.
2. **Flatten and halt strategy.** The affected strategy is moved to
   `auto_disabled` per `00_master_architecture.md:179`. Open positions
   for that strategy flatten via `MARKET` with `ExitReason.OVERRIDE`.
3. **Global kill-switch.** Whole-system halt per
   `11_execution_layer_spec.md` "Global kill-switch."

| Trigger                                                       | Action                       |
|---------------------------------------------------------------|------------------------------|
| Single feature `degraded` for a strategy that consumes it     | No-new-entry for that strategy |
| Single feature `stale` or `failed`                            | Flatten and halt that strategy |
| Live-vs-historical drift CRITICAL on a consumed feature       | Flatten and halt that strategy |
| Spec retirement-criteria CRITICAL trigger fired               | Flatten and halt that strategy |
| Daily loss cap breach                                         | Global kill-switch           |
| Position-vs-broker divergence not resolved in 30 s            | Global kill-switch           |
| Broker disconnect ≥ 30 s during RTH                           | Global kill-switch           |
| Three or more strategies tripped within 5 minutes             | Global kill-switch           |
| Time skew > 2 s                                               | Global kill-switch           |

### Cooldown and restart

After a no-new-entry or strategy-halt, restart requires:

- The triggering check returning to `green` for ≥ 5 consecutive minutes.
- For strategy-halt: per `00_master_architecture.md:179`, re-enable
  requires fresh validation. The monitoring layer surfaces the
  re-enable request; `13_governance_process.md` defines who approves.

After a global kill-switch, restart follows
`11_execution_layer_spec.md` "Restart contract."

---

## Feature drift detection

Per `02_feature_pipeline.md:593-597`. Operationalization:

### Method

For every feature consumed by a live spec:

1. Maintain a rolling reference window of the last 30 days of
   historical-mode values (computed offline, refreshed weekly).
2. Maintain a rolling live window of the last 60 minutes of live values.
3. Compare distributions:
   - **Two-sample KS test** between live and reference windows.
   - **Median shift** in standard deviations of the reference.
   - **Tail-mass shift** at p95 and p5 of the reference.
4. Run the comparison every 60 s during RTH.

### Thresholds

| Condition                                          | Severity   |
|----------------------------------------------------|------------|
| KS p-value < 0.05                                  | INFO       |
| KS p-value < 0.01                                  | WARNING    |
| KS p-value < 0.001 sustained for 10 consecutive minutes | CRITICAL |
| Median shift > 2σ                                  | WARNING    |
| Median shift > 4σ                                  | CRITICAL   |

When any feature trips WARNING, the registry health enum for that
feature transitions to `degraded`. CRITICAL transitions it to `failed`.
Selection layer reads the enum per `09_selection_layer.md:253` and
adjusts.

### What this is not

Drift detection is not a regime classifier. A regime change that the
strategy's `context_conditions` should already filter for is not a
monitoring event — it is a selection-layer event.

---

## Model freshness

Per-model `staleness_threshold` declared at registration time. Model
freshness check compares `now - last_retrained` against the threshold.

| Condition                                         | Severity   |
|---------------------------------------------------|------------|
| `last_retrained` within threshold                 | green      |
| Threshold exceeded by ≤ 50%                       | INFO       |
| Threshold exceeded by 50–100%                     | WARNING    |
| Threshold exceeded by > 100%                      | CRITICAL — model output marked `stale`; dependent specs no-new-entry |

Retraining cadence and ownership live in the model registry, not here.
Monitoring's job is to surface the staleness, not to retrain.

---

## Selection-layer integration

### Thesis drift alerts

Per `09_selection_layer.md:251` and `:321`. The selection layer pins the
original thesis on every position open. On every cycle, it compares its
current view of the trade against the pinned thesis and emits a
divergence score.

| Divergence score                  | Severity   |
|-----------------------------------|------------|
| Within tolerance                  | green      |
| Outside tolerance, single cycle   | INFO       |
| Outside tolerance, two cycles     | WARNING    |
| Outside tolerance, three cycles   | CRITICAL — strategy-level flatten suggested; operator confirms |

### Selection-cycle health

| Condition                                                     | Severity     |
|---------------------------------------------------------------|--------------|
| Cycle ran within 1× cadence                                   | green        |
| Cycle ran within 1–2× cadence                                 | INFO         |
| Cycle ran 2–4× cadence                                        | WARNING      |
| Cycle ran > 4× cadence or skipped entirely                    | CRITICAL — fall back to previous decision per `09_selection_layer.md:254` |

### Override discipline

Human overrides are logged but not alerted on individually. A weekly
report tracks override rate; outliers feed back to
`13_governance_process.md`.

---

## Loss-cap enforcement

### Daily loss cap

- Computed continuously as realized P&L since session open + open-
  position mark-to-market.
- Cap value is operator-set per account; default to 2× the maximum
  per-trade R that any active spec is sized to.
- Breach action: global kill-switch (`11_execution_layer_spec.md`).
- Reset at session start. Yesterday's loss does not consume today's
  cap, but two consecutive daily breaches escalate to weekly.

### Weekly loss cap

- Rolling 5-session realized P&L.
- Cap value default 5× the maximum per-trade R of any active spec.
- Breach action: global kill-switch + governance review before next
  session start.

### Per-strategy loss cap

- Rolling 10-trade realized R per strategy.
- Cap declared in the spec under
  `04_strategy_spec_schema.md:80-94` mutable fields.
- Breach action: flatten and halt that strategy.

---

## Live-replay drift process

Per `05_backtest_harness.md:41` (deferred entry on the harness side).
This spec defines the monitoring view of it.

Daily, after session close, a scheduled job:

1. Pulls the day's `runs/{run_id}/` artifact set
   (`11_execution_layer_spec.md` "Deterministic replay contract").
2. Re-runs the harness in `single` mode against the same coverage
   window with the spec versions captured in `meta.json`.
3. Diffs harness-produced fills against `fills.jsonl`.
4. Diffs harness-produced exit reasons against the live exit reasons.
5. Emits a daily replay report:
   - Per-trade fill price divergence distribution.
   - Per-strategy expectancy divergence.
   - Any structural divergences (orders that exist in one and not the
     other).

### Severity

| Condition                                                       | Severity     |
|-----------------------------------------------------------------|--------------|
| All trades within tolerance                                     | INFO (logged) |
| > 5% of trades exceed tolerance                                 | WARNING      |
| Structural divergence (missing orders / extra orders)           | CRITICAL     |
| Per-strategy expectancy divergence > declared model error       | WARNING; CRITICAL on second occurrence |

The threshold "declared model error" lives in
`11_execution_layer_spec.md` "Replay parity tolerance" (open decision).

---

## Operational playbooks

Each playbook describes the degraded mode and the response. Stored
adjacent to this spec for operator quick-reference.

### LLM unavailable

- Selection layer holds previous cycle's decision per
  `09_selection_layer.md:254`.
- After 3 consecutive failed cycles: CRITICAL; move all active
  strategies to DORMANT (per `09_selection_layer.md:254`); existing
  protective stops remain working.
- Recovery: when LLM responsive again, resume normal cadence; hold a
  conservative cycle to confirm context unchanged before re-arming.

### Feature stale or failed

- Strategy-level flatten and halt for any spec that consumes the
  feature.
- Other strategies unaffected.
- Recovery: feature returns to `green` for ≥ 30 minutes plus
  governance approval to re-arm any affected strategies.

### Broker disconnect

- All `WORKING` orders move to `UNKNOWN` per
  `11_execution_layer_spec.md`.
- New orders rejected.
- After 30 s: global kill-switch.
- Recovery: reconnection + reconciliation pass + operator restart.

### Time skew

- Global kill-switch.
- Recovery: NTP corrected; broker time delta within tolerance for ≥ 5
  minutes; operator restart.

### Loss-cap breach

- Global kill-switch.
- Recovery: governance review per `13_governance_process.md`.
  Re-enabling cap requires explicit human authority — the cap is not
  bypassable by the system itself.

---

## Cross-references

- `00_master_architecture.md:179` — auto-disable as a governance event.
- `00_master_architecture.md:206` — hard cap enforcement intent.
- `02_feature_pipeline.md:593-597, 628-639` — feature health enum and
  drift framing.
- `05_backtest_harness.md:41` — live-replay drift detection (harness
  side; deferred).
- `08_development_pipeline.md:232-248` — Stage 9 monitoring activities
  this spec implements.
- `09_selection_layer.md:251-256` — failure modes the selection layer
  handles; this spec defines the alert surface.
- `11_execution_layer_spec.md` — kill-switch, reconciliation, replay
  artifact.
- `13_governance_process.md` — re-enable authority.
- `src/tradegy/live/base.py:81-83` — `LiveAdapter.health()` contract.
- `src/tradegy/features/engine.py` — feature publication path.
- `src/tradegy/harness/stats.py` — aggregate-stat schema reused for
  realized-vs-envelope comparison.

---

## Resolved decisions

These were open at draft time and are now resolved. Each carries a
short rationale; the chosen behavior is binding for v1.

1. **Notification transport — push + SMS for v1.** Single-operator
   deployment uses push + SMS for CRITICAL and a 1-hour digest for
   WARNING. PagerDuty-equivalent rotation is a hard prerequisite for
   the multi-operator transition, not for v1.
2. **Drift method — KS + median + tail in v1.** Refactor to a unified
   per-feature-class metric (Wasserstein for continuous, JS for
   categorical) is deferred until the first model-backed feature
   ships, at which point a single calibrated metric becomes more
   valuable.
3. **Reference window — 30-day rolling plus quarterly anchor
   snapshot.** The rolling window catches fast drift; the anchor
   snapshot (refreshed once per quarter from the same offline pipeline)
   catches slow regime drift the rolling window would itself absorb.
   Both are compared on every drift check.
4. **Auto-restart authority — none.** No silent auto-restart. Every
   halt clears via human acknowledgment, even when the underlying
   trigger has already returned to `green`. Acknowledgment is cheap;
   silently re-arming after a flap is the higher risk.
5. **Per-strategy loss-cap — both windows.** The cap trips when
   either: 10-trade rolling realized R or 5-session rolling realized R
   exceeds the declared envelope floor. Whichever fires first
   triggers the strategy-level halt.
6. **Replay tolerance.** Resolved in
   `11_execution_layer_spec.md` "Resolved decisions" item 8.
   Per-trade ≤ 1 tick AND Wasserstein ≤ declared
   `replay_tolerance_R` (default 0.1). Re-stated here for parity.
7. **Quiet-hours suppression — none.** Dashboards stay live
   between sessions. Off-session anomalies (data-feed issues,
   reconciliation drift, registry-health regressions) still need to
   surface immediately. Daily digests summarize the session for
   review.
8. **Backtest-side monitoring — stress replay only.** Sanity-check and
   walk-forward backtests stay alert-free. Stress-period replay
   (deferred per `05_backtest_harness.md:36`) DOES surface would-be
   alerts so degraded-mode behavior gets exercised before live.

## Still open

- Single-metric drift refactor — re-evaluate when the first
  model-backed feature is registered.
