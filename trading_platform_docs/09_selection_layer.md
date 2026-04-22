# Selection Layer Spec

**Status:** Draft for review
**Purpose:** Define the runtime LLM-supervised layer that decides which library strategies are active, manages open positions at the thesis level, and produces session-bounding artifacts. The selection layer sits above the tactical layer and is not in the critical path of individual trade execution.

---

## Design principles

1. **Strategic, not tactical.** The selection layer decides *what game* the system is playing — which strategies are armed, what the playbook is, what we stand down from. It does not place individual orders, calculate stops, or manage tick-by-tick.

2. **Cadence + events, not continuous.** Runs on a 5–10 min cycle with event-triggered interruptions. Not a streaming system.

3. **Bounded action space.** LLM output is validated against a strict schema and deterministic guardrails. Output outside the action space is rejected.

4. **Reads from the library, never writes.** Cannot invent strategies, change parameters outside declared envelopes, or bypass strategy-level safety rules.

5. **Structured context in, structured decision out.** Natural language in rationale fields only; operational fields are all structured.

6. **Explicit stand-down.** Doing nothing is a valid first-class decision. The `stand_down` strategy class exists to make this a concrete selection, not an absence.

7. **Auditable.** Every decision logged with full context, candidates considered, choice made, rationale. Post-hoc evaluation of decision quality is a first-class concern.

---

## Inputs

Assembled fresh at the start of each cycle:

### Market context snapshot

- Current price, session shape so far (range, volume, VWAP, current vs opening)
- Regime state (HMM probabilities, trend strength metrics, volatility regime)
- Volatility indicators (VIX level, VIX term structure, realized vs implied)
- Cross-asset state (DXY, TLT, oil, gold — current and deltas)
- Breadth indicators (advance/decline, sector rotation)
- Options-derived features where available (put/call, IV skew, 0DTE positioning)
- Time markers (time since open, time to close, day-of-week, days-to-expiry)

### Event calendar state

- Scheduled events today (FOMC, CPI, NFP, earnings, Fed speakers)
- Minutes to nearest high-impact event
- Past events today (for post-event context)
- Blackout windows active per each library strategy

### Library state

- All `live`-tier strategies with their `context_conditions`, `tier`, `enabled` flags
- Current state per strategy (DORMANT, ARMED, IN_POSITION, etc.)
- Recent performance per strategy (last N trades, envelope status)

### Portfolio state

- Open positions with entry context, current P&L, time in trade
- Original thesis per open position (pinned at entry, for drift detection)
- Risk envelope remaining (daily loss capacity, max concurrent positions, margin headroom)
- Incompatibility constraints active

### Session state

- Today's P&L so far
- Consecutive losses or wins today
- Prior strategy activations and their outcomes this session
- Playbook changes this session (hysteresis budget)

### Previous cycle's output

- What was decided last cycle
- Rationale of last decision
- Expected next reeval trigger (time or condition)

---

## Decision logic

### Step 1: Hard filtering (deterministic, pre-LLM)

Mechanically eliminate strategies that cannot possibly run right now:

- `enabled = false` strategies removed
- Blackout windows active for the strategy → removed from candidates
- Strategy currently `DONE_FOR_SESSION` and single-attempt → not a candidate
- Strategy auto-disabled by monitoring → removed
- Strategy incompatible with currently open-position strategies → removed (unless the existing position is exiting)

Output: filtered candidate set plus hard-filter log (which strategies eliminated, why).

### Step 2: LLM scoring (structured)

LLM receives: current context, filtered candidates with their context conditions, open positions with original theses, portfolio state, previous cycle's output.

Produces structured output:

```yaml
selection_decision:
  cycle_id: "..."
  cycle_timestamp: "..."
  context_summary: "..."          # LLM's structured read of current context

  active_playbook:                 # strategies to ARM or keep armed
    - strategy_id: "..."
      rationale: "..."             # why this fits now
      confidence: 0.0-1.0
      expected_conditions: "..."   # what the LLM expects to hold during armed window

  stand_down:
    active: true|false
    rationale: "..."

  rejected_candidates:             # filtered-in strategies NOT being armed
    - strategy_id: "..."
      rationale: "..."

  watch_list:                      # not armed, monitor for later activation
    - strategy_id: "..."
      watch_condition: "..."       # what change would cause activation

  open_position_actions:
    - position_id: "..."
      action: hold|tighten_stop|take_partial|exit_market|flag_for_human
      parameters: {...}
      rationale: "..."
      thesis_status: intact|weakening|broken

  next_reeval:
    trigger: time|price|event|feature_threshold
    parameters: {...}

  overall_confidence: 0.0-1.0
  notes_to_human: "..."            # anything worth flagging for review
```

### Step 3: Guardrail enforcement (deterministic, post-LLM)

Validate LLM output against hard rules:

- **Incompatibility check.** Proposed active set respects pairwise `incompatible_with` and portfolio-level composition constraints.
- **Risk envelope check.** Combined risk across proposed active set within `operational.risk_envelope` bounds and portfolio-level cap.
- **Auto-disabled check.** No auto-disabled strategy proposed.
- **Hysteresis check.** No strategy armed/disarmed more than once in the hysteresis window (default: 30 min minimum between flips); session cap on total playbook changes (default: 2–3).
- **Position action bounds.** Stop tightenings within the spec's `hard_max_distance_ticks`; partials at declared fractions only; no direction flips.
- **Schema compliance.** All required fields present, all enums valid, all referenced IDs exist.

On violation: reject LLM output, log the violation, either re-prompt the LLM with the violation flagged or fall back to the previous cycle's decision (configurable per violation type).

### Step 4: Apply

- Strategies transitioning DORMANT → ARMED receive activation signal
- Strategies transitioning ARMED → DORMANT receive deactivation signal
- Open position action commands forwarded to tactical layer
- Next reeval trigger scheduled
- Full decision record persisted to audit log

---

## Cadence and event triggers

### Base cadence

Default: **every 5 minutes during RTH**, with specific exceptions:
- No cycles in the first 5 minutes of session (opening noise)
- No cycles in the last 10 minutes of session (closing noise)
- Reduced or paused during declared blackout windows

### Event triggers (interrupt cadence)

Force an off-cycle reeval when:
- Major scheduled event about to release (T minus 2 minutes)
- Major scheduled event just released (T plus 30 seconds)
- Price moves beyond configured threshold within a bar (e.g., 0.3% intra-bar move)
- Open position hits configured thresholds (unrealized P&L, time in trade)
- Strategy-level invalidation condition nearly triggered (selection can preemptively exit)
- Feature registry publishes a regime-change signal
- Human-initiated manual trigger

### Pre-session and post-session bookends

- **Pre-session brief** (once, before RTH open): LLM ingests overnight news, calendar, regime state, correlations. Drafts opening playbook. Human review during early life of live system.
- **Post-session review** (once, after close): LLM evaluates decisions against outcomes. Proposes library-level observations (not library changes — those go through the hypothesis system).

These bookends are often the highest-leverage LLM calls of the day.

---

## Thesis management for open positions

Every open position has a pinned **original entry thesis** — the LLM's stated reason for allowing this strategy to take this trade, captured at entry time. Each selection cycle compares the current thesis view against the pinned original.

Thesis statuses:

- **`intact`** — the reasoning for the position still holds; continue as planned
- **`weakening`** — reasoning is under stress; consider partials or stop tightening
- **`broken`** — reasoning no longer applies; exit

**Drift detection:** if the current thesis view diverges from the pinned original in meaningful ways *without* the LLM flagging it, that's an alert. The purpose of pinning is to prevent slow rationalization of losing positions.

---

## What the selection layer does NOT do

- Invent new strategies
- Change strategy parameters
- Bypass strategy-level safety rules (blackouts, hard stops)
- Place orders directly
- Manage ticks or stops at mechanical level
- Make decisions about capital allocation across instruments (currently single-instrument)
- Train or update models
- Modify retirement criteria
- Approve strategy promotions to live

All of the above are either tactical-layer responsibilities or governance-layer decisions.

---

## Evaluation criteria

Three measurable dimensions, evaluated post-session and aggregated over time:

### Coverage

When current conditions match a library strategy's declared envelope, did the selection layer activate it?
- Miss rate: fraction of "fitting" sessions where no matching strategy was activated
- False-miss rate: activations declined that should have been made (identified post-hoc from actual market behavior)

### Precision

When a strategy was activated, did conditions remain within its envelope?
- Drift rate: fraction of activations where conditions shifted out of envelope mid-activation without selection layer noticing
- Wrong-context rate: activations in conditions clearly outside the envelope

### Stand-down discipline

On days with no clearly-fitting strategy, did the system stand down?
- Over-activation rate: fraction of marginal-fit sessions where the system activated strategies that shouldn't have been activated
- Stand-down consistency: does the system stand down in similar-shaped unfitting conditions?

### Decision quality

- Hit rate of `EXIT_NOW` overrides: did flagged exits precede adverse moves?
- Hysteresis discipline: playbook changes per session within target range
- Thesis drift detection rate: how often does the system catch drifting theses before they turn into full losses?

---

## Failure modes and mitigations

| Failure mode | Mitigation |
|---|---|
| Thrashing (flipping playbook every cycle) | Hysteresis: minimum duration between flips; session cap on flips |
| Narrative drift on open positions | Pinned original thesis; drift alerts when current view diverges |
| Over-activation | Explicit `stand_down` as a selection; track activation rate; tune LLM prompt toward conservatism |
| Context manipulation via bad features | Feature fidelity monitoring; selection layer receives fidelity tier per feature it uses; can reject decisions built on compromised features |
| LLM unavailable | Fallback: maintain previous cycle's decision; if sustained outage, move all active strategies to DORMANT and flag human |
| LLM output fails validation repeatedly | Escalate to human after N consecutive failures; fall back to previous cycle |
| Silent divergence between selection reasoning and actual strategy behavior | Post-session review explicitly compares rationale against outcomes; patterns trigger review |

---

## Integration points

### Receives from:
- Feature pipeline: real-time feature stream (context snapshot inputs)
- Library: live-tier specs with `context_conditions` and risk envelopes
- Tactical layer: strategy state transitions, fill confirmations
- Execution layer: portfolio state, risk-envelope headroom
- Monitoring: strategy envelope status, auto-disable flags
- Previous cycle: decision history

### Writes to:
- Tactical layer: strategy arm/disarm commands, position action commands
- Audit log: full decision records per cycle
- Monitoring: selection decision telemetry
- Hypothesis system (via post-session review): observations about library gaps or coverage issues

---

## Human oversight tiers

Mirrors the per-strategy `tier` field but at the selection-layer level:

- **`full_human_review`** (initial live operation): every selection decision confirmed by human before application. Slow but safest.
- **`confirm_major_changes`**: auto-apply continuation of previous cycle; require human confirm for changes (arming, disarming, open position actions).
- **`alert_on_anomaly`**: auto-apply all decisions; alert human on decisions with low confidence, flagged anomalies, or overrides proposed.
- **`fully_autonomous`**: auto-apply everything; human reviews via post-session log.

Graduation through tiers based on demonstrated decision quality. Initial live operation stays at `full_human_review` or `confirm_major_changes` for a defined period regardless of statistics.

---

## LLM prompt structure (sketch)

```
SYSTEM:
You are the selection layer of a quantitative futures trading system. Your job
is to decide which registered library strategies should be active right now,
and how to manage any open positions, based on current market context and each
strategy's declared conditions. You select from the library; you do not invent
strategies or change their mechanics.

CURRENT CONTEXT:
{structured context snapshot}

CANDIDATE STRATEGIES (post hard-filter):
{for each: id, mechanism summary, context_conditions, recent envelope status}

OPEN POSITIONS:
{for each: strategy id, entry time, entry price, current P&L, pinned original thesis}

PORTFOLIO STATE:
{risk envelope, incompatibilities, session-level state}

PREVIOUS CYCLE:
{last decision and rationale}

RULES:
- Output must conform to the selection decision schema
- Respect incompatibility constraints
- Respect hysteresis (no flip within 30 min of previous flip; max 3 playbook changes per session)
- Prefer stand_down when no candidate clearly fits
- Flag drift on open positions if your current read diverges from the pinned thesis

TASK:
Produce a selection decision for this cycle.
```

Output validated by the guardrail layer before application.

---

## Open design decisions

1. **LLM model selection.** Decision quality matters; cost-per-call matters (hundreds of calls per day). Likely use strongest model for pre-session brief and post-session review, mid-tier for intra-session cycles, with auto-escalation on low-confidence outputs.

2. **Cycle frequency tuning.** 5 minutes is a starting point. May need to be slower (10 min) to reduce LLM cost and cycle thrash, or faster (2 min) during volatile conditions. Propose: adaptive, with cycle interval modulated by realized vol and event proximity.

3. **Pre-session brief ↔ intra-session consistency.** How much should intra-session cycles defer to the pre-session brief's overall call vs reassess independently? Too much deference = missed regime changes; too little = thrashing. Propose: brief sets priors and risk envelope; intra-session cycles can diverge but must articulate why.

4. **Candidate context vs full library.** Does the LLM see only the post-filter candidate set, or also the rejected set (for broader awareness)? Full context better for reasoning; larger token cost. Propose: filter set by default, full library at session boundaries.

5. **Thesis pinning granularity.** Per-position or per-activation-window? If a strategy is armed, disarmed, and re-armed in the same session, does the thesis reset? Propose: thesis is per-activation-window, with prior thesis referenced for continuity.

6. **Override workflow for humans.** How does a human override a selection decision during live? Needs a fast approval/rejection UI with logged rationale. Design owed.
