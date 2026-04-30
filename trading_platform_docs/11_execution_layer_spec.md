# Execution Layer Spec

**Status:** Draft for review
**Purpose:** Define the deterministic broker-facing execution layer that
sits between the tactical layer and Interactive Brokers. The execution
layer owns the order lifecycle, broker reconciliation, account state, and
hard risk enforcement. It contains no intelligence — every decision about
*whether* to trade lives upstream. This spec turns the intent statements
in `00_master_architecture.md:65-71` into an enforceable contract.

---

## Why this spec exists

`10_review_gap_analysis.md:28-41` flags the missing execution-layer
specification as a P0 blocker:

> The architecture depends on an execution layer for order lifecycle,
> broker reconciliation, and hard risk enforcement, but this is listed as
> "not yet documented." Without this spec, all upstream guarantees are
> non-binding in production.

Backtest-side fill semantics already exist in
`src/tradegy/harness/execution.py` (CostModel, `fill_market_order`,
`fill_stop_at_price`). Live-side adapters exist as connection plumbing in
`src/tradegy/live/base.py` and `src/tradegy/live/ibkr.py`. Neither side
has a documented **order lifecycle contract**. This spec is the contract.

---

## Scope

### Owned by the execution layer

- Order submission, modification, cancellation
- Fill receipt and dispatch back to the tactical layer
- Position reconciliation against broker
- Account-state reads: balance, margin, buying power
- Portfolio-level hard risk caps (daily/weekly loss, max concurrent
  positions) — enforced regardless of upstream decisions, per
  `00_master_architecture.md:206`
- Session-boundary flatten enforcement
- Global kill-switch

### Not owned by the execution layer

- *Whether* to trade. Strategy and selection layers decide.
- Strategy-level stop placement, sizing, exit timing.
- Feature computation or context building.
- LLM calls of any kind. The execution layer never reads or invokes an
  LLM.

### Boundary with the tactical layer

The tactical layer emits an `Order` (see
`src/tradegy/strategies/types.py:69-80`). The execution layer is
responsible for everything that happens between that emission and a
`Fill` flowing back. The `Order` schema is the contract surface.

---

## Order lifecycle state machine

Every order moves through this state machine. States are persisted; the
state plus the transition log is sufficient to reconstruct any order's
history.

| State        | Meaning                                                         | Terminal |
|--------------|-----------------------------------------------------------------|----------|
| `PENDING`    | Created locally, idempotency key assigned, not yet sent         | no       |
| `SUBMITTED`  | Sent to broker, awaiting acknowledgment                         | no       |
| `WORKING`    | Acknowledged by broker, live in the order book                  | no       |
| `PARTIAL`    | One or more partial fills received, remaining quantity working  | no       |
| `FILLED`     | Total quantity filled                                           | yes      |
| `CANCELLED`  | Cancelled (by us, by broker, or by session-end policy)          | yes      |
| `REJECTED`   | Broker rejected the order                                       | yes      |
| `EXPIRED`    | Time-in-force elapsed without fill                              | yes      |
| `UNKNOWN`    | Reconciliation cannot determine state — escalation required     | no       |

### Allowed transitions

```
PENDING   ──► SUBMITTED ──► WORKING ──► FILLED
                                  │
                                  ├──► PARTIAL ──► FILLED
                                  │              │
                                  │              └──► CANCELLED
                                  ├──► CANCELLED
                                  └──► EXPIRED

PENDING   ──► REJECTED   (pre-flight fail; never sent)
SUBMITTED ──► REJECTED   (broker rejects on receipt)
ANY       ──► UNKNOWN    (broker silent past timeout; reconciliation owns)
UNKNOWN   ──► WORKING|FILLED|CANCELLED|REJECTED  (reconciliation resolves)
```

A transition is recorded with `(order_id, from_state, to_state, ts_utc,
reason, source)` where `source ∈ {local, broker, reconciliation,
operator}`. The transition log is append-only and replayable.

---

## Idempotency keys

Every `Order` carries a deterministic, broker-namespace-unique
`client_order_id` generated at order creation. The format:

```
{strategy_id}:{session_date}:{intent_seq}:{role}
```

- `strategy_id` — spec id from `04_strategy_spec_schema.md`.
- `session_date` — `YYYYMMDD` UTC of the current CMES session.
- `intent_seq` — monotonically increasing per strategy per session.
  Resets at the session boundary (consistent with the harness session
  reset at `05_backtest_harness.md:19`).
- `role` — one of `entry`, `stop`, `target`, `flatten`,
  `risk_override`. Disambiguates orders that share `(strategy,
  session, seq)` when a single intent emits multiple orders (e.g.,
  bracket).

### Dedup window

If a duplicate `client_order_id` is presented within 24 h, the execution
layer rejects the second submission and returns the prior order's status.
Beyond 24 h, IDs may rotate freely (sessions are bounded; cross-session
collisions are not possible by construction).

### Retry semantics

A submission that fails with a network/transient error retries up to 3
times with exponential backoff (250 ms, 500 ms, 1 s). The
`client_order_id` does not change across retries. After 3 failures the
order moves to `UNKNOWN` and the reconciliation loop owns resolution.

---

## Order types

| Type        | v1 status              | Notes                                                              |
|-------------|------------------------|--------------------------------------------------------------------|
| `MARKET`    | shipped (backtest)     | Fills at next-bar-open + slippage in backtest; broker market live  |
| `STOP`      | shipped (backtest)     | Fills at stop ± slippage when bar trades through                   |
| `LIMIT`     | deferred contract      | Defer per `05_backtest_harness.md:23` until a spec needs it        |
| `STOP_LIMIT`| deferred contract      | Not in `OrderType` enum; add when first needed                     |

The enum lives at `src/tradegy/strategies/types.py:26-29`. Adding a type
requires (a) extending `OrderType`, (b) extending the harness fill
function set, (c) extending the live adapter's order builder, (d) adding
a parity test that exercises the type in both backtest and live-replay.

### Stop-loss orders

A strategy submits its protective stop as a separate `STOP` order
immediately after the entry fill (per
`00_master_architecture.md:159`). The execution layer treats it as a
normal order through the lifecycle above. If the stop is `WORKING` and
the entry is later cancelled (race condition), the execution layer
auto-cancels the orphaned stop within 1 s and emits a divergence event.

---

## Reject and partial-fill policy

### Default behavior (no strategy override)

| Event                              | Default action                                                      |
|------------------------------------|---------------------------------------------------------------------|
| Pre-flight reject (margin, hours)  | Order moves to `REJECTED`. Strategy notified. No retry.             |
| Broker reject on submit            | Same as pre-flight.                                                 |
| Partial fill, more time available  | Stay in `PARTIAL`. Continue working remaining quantity.             |
| Partial fill, time-in-force elapsed | Cancel remainder. Strategy receives the partial as the realised fill. |
| Stop order partial fill            | Cancel remainder immediately and submit market order for residual.  |

### Strategy override

A spec MAY declare an `execution.partial_fill_policy` field with one of:

- `accept_partial` — same as default, accept whatever fills.
- `cancel_remainder` — cancel any unfilled quantity after first fill.
- `flatten_on_partial` — cancel remainder and immediately market-flatten
  the partial position.

Default is `accept_partial`. The override field is optional per
`04_strategy_spec_schema.md:548-573` (which already establishes the
strategy/execution separation principle).

---

## Timeout and cancel-replace

### Time-in-force

All v1 orders default to `DAY` (cancelled at session end). Strategy
specs MAY declare `execution.time_in_force` with one of:

- `IOC` — immediate-or-cancel; fill what's available now, cancel rest.
- `FOK` — fill-or-kill; fill in full or cancel.
- `DAY` — default.
- `GTC` — good-till-cancelled; **rejected** in v1 (would survive
  session-flatten and contradict `05_backtest_harness.md:19`
  invariants). Add only with explicit governance review.

### Cancel-replace

A strategy MAY cancel and re-submit by emitting a fresh `Order` with a
new `client_order_id`. The execution layer does NOT support amend-in-
place in v1; it always cancels first, waits for `CANCELLED`
acknowledgment, then submits the replacement. The `intent_seq` of the
replacement is one higher than the cancelled order.

---

## Broker reconciliation loop

Runs continuously while the live system is connected. Cadence:

| Frequency            | Check                                                              |
|----------------------|--------------------------------------------------------------------|
| Every 1 s            | Open-order state diff (local vs broker)                            |
| Every 5 s            | Position quantity diff per instrument                              |
| Every 30 s           | Account balance, available funds, margin diff                      |
| Every 60 s (or after fill) | Realised P&L since session open                              |

### Divergence resolution

| Divergence                                                  | Resolution                                                |
|-------------------------------------------------------------|-----------------------------------------------------------|
| Local order `WORKING`, broker has no record                 | Move to `UNKNOWN`; query order list with retries; if still absent after 5 s, mark `REJECTED` and notify strategy |
| Local order `FILLED`, broker shows `WORKING`                | Trust broker; reopen local order; investigate fill source |
| Local position 1L, broker position 0                        | Trust broker; flatten local; emit CRITICAL alert          |
| Local position 0, broker position 1L                        | Trust broker; submit market order to flatten broker side; emit CRITICAL alert |
| Account margin breached                                     | Block new orders; trigger session-end flatten; CRITICAL  |

The execution layer NEVER silently overwrites broker state. The broker
is the source of truth for positions and fills. Local state catches up.
The only exception is the kill-switch path below.

---

## Account state and margin

Pre-order checks (executed in this order, all must pass):

1. **Connection healthy.** Broker connected, last heartbeat within 2 s.
2. **Within trading hours.** CMES session active for the instrument.
3. **Daily loss cap.** Realized + open P&L ≥ negative of declared cap.
4. **Concurrent position cap.** Adding this order would not exceed the
   max-concurrent-positions cap configured at the operator layer.
5. **Margin sufficient.** Estimated post-fill maintenance margin ≤
   available funds. Estimate uses the broker's reported initial-margin
   per contract; if that read fails, the order is rejected.
6. **Strategy-level enabled.** Spec's operational tier is not
   `proposal_only`; spec is not auto-disabled.

A failed pre-flight check produces `REJECTED` with a structured reason
code. The order never leaves the local boundary.

Post-order: margin is re-evaluated on every fill. A breach triggers the
kill-switch path below.

---

## Session boundary behavior

Aligned with the harness session model at
`05_backtest_harness.md:19`. At each session end:

1. Cancel all `WORKING` and `PARTIAL` orders for the instrument.
2. For any non-flat position, submit a `MARKET` order with `tag =
   "session_end"`. The exit is recorded with
   `ExitReason.SESSION_END` (`src/tradegy/strategies/types.py:37`).
3. After flatten confirmation, reset `intent_seq` for every strategy
   to 0.
4. If session-end flatten cannot complete within 30 s, escalate to
   `UNKNOWN` and trigger the kill-switch.

The execution layer does NOT carry positions across sessions in v1.
Overnight holding is out of scope and would require margin-cost
modeling per `05_backtest_harness.md:26`.

---

## Global kill-switch

A single boolean controls execution-layer behavior. When set, the
execution layer:

1. Rejects every new `Order` with reason `kill_switch_active`.
2. Immediately cancels every `WORKING`/`PARTIAL` order.
3. For any non-flat position, submits a `MARKET` flatten with `tag =
   "kill_switch"`. ExitReason recorded as
   `ExitReason.OVERRIDE`.
4. Holds the kill state until explicit operator clear.

### Trigger authority

| Trigger                                                | Authority                              |
|--------------------------------------------------------|----------------------------------------|
| Daily loss cap breach                                  | Automatic (execution layer self-trip)  |
| Margin call from broker                                | Automatic                              |
| Position-vs-broker divergence not resolved in 30 s     | Automatic                              |
| Operator command                                       | Authenticated CLI invocation           |
| Auto-disable cascade (≥3 strategies tripped at once)   | Automatic                              |

### Restart contract

After a kill, restart requires:

1. Operator confirmation that the underlying issue is understood.
2. A fresh reconciliation pass (positions, margin, open orders) before
   accepting any new submissions.
3. Audit-log entry recording who restarted, when, why.

This is consistent with `00_master_architecture.md:179` (auto-disabled
strategies need fresh validation before re-enable).

---

## Failure semantics

| Condition                          | Behavior                                                        |
|------------------------------------|-----------------------------------------------------------------|
| Broker socket disconnect           | All `WORKING` orders move to `UNKNOWN`. New orders rejected. Reconnect and reconcile before resuming. |
| Broker rate limit                  | Order moves to `PENDING`; retry path engages with backoff.      |
| Stale account state (read fails)   | Block new orders; existing protective stops remain working.     |
| IBKR server reset                  | Treat as disconnect; full reconciliation on reconnect.          |
| Local process crash                | On restart, replay the transition log; reconcile against broker before accepting new orders. |
| Time skew > 2 s vs broker          | Block new orders; emit CRITICAL alert; rely on existing stops.  |

Degraded modes are intentionally restrictive: when state is unclear,
the layer stops opening new risk and lets existing protective stops
protect the book.

---

## Backtest ↔ live parity contract

The principle from `05_backtest_harness.md:69` is that backtest and
live share execution code. This spec operationalizes the contract:

### Shared

- `Order`, `Fill`, `Position`, `Side`, `OrderType`, `ExitReason`
  (`src/tradegy/strategies/types.py`).
- `CostModel` parameters (`src/tradegy/harness/execution.py:27-36`)
  must match the live broker's actual cost realities to within tolerance
  declared in the spec.
- Order lifecycle state machine (this doc). Live progresses through it
  driven by broker events; backtest progresses through it driven by bar
  iteration.
- `client_order_id` format is computed identically in both modes.
- Reject reasons map 1:1.

### Differs (and is documented)

| Aspect                        | Backtest                                          | Live                                  |
|-------------------------------|---------------------------------------------------|---------------------------------------|
| Fill timing                   | Next-bar-open with fixed-tick slippage            | Broker-determined                     |
| Stop fill                     | At-stop ± slippage when bar's range trades through| Broker-determined                     |
| Reject probability            | Zero (no broker present)                          | Real, modeled in stress runs          |
| Partial fills                 | None in MVP                                       | Real, handled per policy              |
| Reconciliation loop           | No-op (no broker)                                 | Active                                |
| Margin checks                 | No-op (no account)                                | Active                                |

Stress-period replay (deferred per
`05_backtest_harness.md:36`) SHOULD inject simulated broker rejects,
partial fills, and disconnects to exercise the live-only paths.

---

## Deterministic replay contract

Every live trading day produces a replayable artifact:

```
runs/{run_id}/
  ├── orders.jsonl              # one line per order, full transition log
  ├── fills.jsonl               # one line per fill, broker-confirmed
  ├── positions.jsonl           # snapshot every reconciliation tick
  ├── account.jsonl             # snapshot every 30 s
  ├── alerts.jsonl              # all alerts and severities raised
  ├── kill_switch.jsonl         # any trips and clears
  └── meta.json                 # spec ids, versions, broker session info
```

### Replay invariants

Given the day's `orders.jsonl` and the corresponding bar feed:

1. The harness in `single` mode, with the same spec versions and
   `CostModel`, MUST produce a fill set whose order-by-order divergence
   from `fills.jsonl` is ≤ the declared parity tolerance.
2. The exit-reason distribution (`ExitReason`) MUST match.
3. The `client_order_id` set MUST match exactly.

These invariants are checked by a planned `tradegy validate-replay`
command (deferred — depends on Stage 6 robustness work). They are the
mechanism by which "backtest is the live system on tape" is enforceable
rather than aspirational.

### What replay does not capture

- Wall-clock latency variability. Modeled separately in monitoring
  (`12_live_monitoring_spec.md`).
- LLM selection-layer reasoning. Lives in the selection-layer audit log
  (`09_selection_layer.md:212`).
- Broker-side queue depth. Outside the execution layer's observable
  surface.

---

## Cross-references

- `00_master_architecture.md:65-71` — execution-layer scope statement.
- `00_master_architecture.md:206` — hard-cap enforcement intent.
- `04_strategy_spec_schema.md:548-573` — strategy/execution separation.
- `05_backtest_harness.md:13-41` — harness implementation status table.
- `05_backtest_harness.md:69` — backtest↔live parity principle.
- `08_development_pipeline.md:188-209` — Stage 7 paper trading.
- `12_live_monitoring_spec.md` — alerts, SLOs, auto-halt that consume
  this layer's outputs.
- `13_governance_process.md` — kill-switch and restart authority.
- `src/tradegy/harness/execution.py` — backtest fill semantics.
- `src/tradegy/live/base.py` — LiveAdapter ABC.
- `src/tradegy/live/ibkr.py` — IBKR adapter (subscribe body deferred).
- `src/tradegy/strategies/types.py` — shared dataclasses.

---

## Resolved decisions

These were open at draft time and are now resolved. Each carries a
short rationale; the chosen behavior is binding for v1.

1. **Bracket order primitive — NO.** Strategies continue to emit entry
   and protective stop as separate `Order`s. The orphan-stop
   auto-cancel-within-1s rule (above) covers the race-condition
   exposure that a bracket would otherwise mitigate; adding a bracket
   primitive would introduce a new contract surface for marginal
   benefit.
2. **Local-vs-broker truth on fill price — broker always wins.** When
   the broker reports a fill price that disagrees with the locally
   modeled execution beyond tolerance, the fill is accepted and the
   divergence is logged at WARNING. The execution layer never rejects
   a confirmed broker fill.
3. **GTC time-in-force — disallowed in v1.** No spec may declare
   `GTC`. Re-enabling requires a cross-session risk model that does
   not yet exist; revisit when overnight margin and roll handling are
   in scope.
4. **Reconciliation cadence — fixed at 1 s / 5 s / 30 s / 60 s.** No
   adaptive cadence in v1. Quiet-hours optimization is premature; the
   constant cadence is simpler to verify and reason about.
5. **Kill-switch granularity — two surfaces.** Global kill-switch
   (this doc) for hard halts; per-strategy halt via the
   `12_live_monitoring_spec.md` auto-disable path. No third tier.
6. **Margin estimate when stale — hard-reject.** When the broker's
   initial-margin read fails or is stale, the order is rejected. The
   layer does not trade on guessed margin.
7. **Order-id collision across IBKR client IDs — namespace suffix.**
   When `IBKR_CLIENT_ID` is non-default, the `client_order_id` format
   becomes `{strategy_id}:{session_date}:{intent_seq}:{role}:c{id}`.
   Default deployment uses one client ID and the suffix is omitted.
8. **Replay parity tolerance — dual-threshold.** A live day's
   `fills.jsonl` is considered parity-consistent with the harness
   re-run when **both** hold:
   - Per-trade fill-price divergence ≤ 1 tick.
   - Distribution-level Wasserstein distance over per-trade R ≤ the
     spec's declared `replay_tolerance_R` (default 0.1).
   This is the same value referenced from
   `12_live_monitoring_spec.md`.

## Still open

- Future re-evaluation of GTC support when a cross-session risk
  model exists.
- Bracket primitive may be revisited if a strategy class arrives
  whose fill semantics genuinely require atomic submission.
