# Governance Process

**Status:** Draft for review
**Purpose:** Operationalize the governance principles already declared
across `00`, `04`, `08`, and `09` into concrete authority assignments,
evidence package standards, checklist templates, and audit-trail
requirements. Governance intent without operational mechanics is not
enforceable — this doc closes that gap.

---

## Why this doc exists

`10_review_gap_analysis.md:56-66` flags missing governance procedure as
a P0 blocker:

> Governance is articulated in principles/tables, but not in procedure.
> Approval authority, evidence package standards, and audit trail
> requirements are not formally operationalized.

What already exists:

- Tier model and a governance authority table
  (`00_master_architecture.md:169-198`).
- Schema mutability rules per field
  (`04_strategy_spec_schema.md:80-94, 391`).
- Human touchpoints by stage
  (`08_development_pipeline.md:316-333`).
- Override and auto-disable references
  (`09_selection_layer.md:212, 318, 343`).

This doc converts those into RACI assignments, checklist templates, and
audit-log requirements.

---

## Scope

### In scope

- RACI matrix for every state change that touches capital or library
  composition.
- Evidence-package standard per decision type.
- Promotion and revision checklist templates.
- Retirement decision criteria.
- Exception handling (fast-track risk reductions, rollback).
- Audit-trail format and retention.

### Out of scope

- Authority for re-enabling halted strategies — this doc defines the
  authority; `12_live_monitoring_spec.md` defines the trigger.
- Cost-budget governance for LLM spend — flagged as P2 in
  `10_review_gap_analysis.md:102-106`, deferred.
- Personnel role definitions for multi-operator deployments. v1
  assumes single owner.

---

## Roles

For v1 single-operator deployment, all roles collapse to one human plus
the system itself. The role names below stay distinct to make the
multi-operator transition a config change, not a redesign.

| Role          | Description                                                       |
|---------------|-------------------------------------------------------------------|
| `Owner`       | Final authority for capital-affecting decisions and library composition |
| `Reviewer`    | Independent reviewer for promotion / revision packets             |
| `Operator`    | Day-to-day runtime supervision; on-call for monitoring alerts     |
| `Author`      | Author of a hypothesis / spec / mechanism document                |
| `System`      | Automated authority for narrowly-scoped events (auto-disable, hard caps) |

In v1, one person carries `Owner`, `Reviewer`, `Operator`, and most
`Author` work. `System` is the codebase. Independence between `Owner`
and `Reviewer` is achieved by a documented cool-off (review at least 24
hours after authoring) — not by a second human.

---

## RACI matrix

R = Responsible, A = Accountable, C = Consulted, I = Informed.

| Decision                                                       | Author | Reviewer | Owner | Operator | System |
|----------------------------------------------------------------|--------|----------|-------|----------|--------|
| New data source admitted (`02_feature_pipeline.md`)            | R      | R        | A     | I        | C (audit) |
| New feature registered for live                                | R      | R        | A     | I        | C |
| Strategy spec promoted to `live`                               | R      | R        | A     | I        | C |
| Tier change `confirm_then_execute` → `auto_execute`            |        | R        | A     | C        | C |
| Tier change `auto_execute` → `confirm_then_execute` (de-grade) |        |          | A     | R        | I |
| Strategy parameter change inside envelope                      | R      | C        | I     | I        | A (validation) |
| Strategy parameter change outside envelope (MAJOR bump)        | R      | R        | A     | I        | C |
| Strategy auto-disabled by monitoring                           |        |          | I     | R        | A |
| Strategy re-enabled after auto-disable                         | R (revalidation) | R | A | C  | C |
| Risk envelope expanded                                         | R      | R        | A     | I        | C |
| Risk envelope reduced (urgent)                                 |        |          | A     | R        | I |
| Daily / weekly loss-cap breach                                 |        |          | I     | R        | A (kill-switch) |
| Loss-cap value modified                                        | R      | R        | A     | I        | C |
| Strategy retired (quantitative trigger)                        |        |          | I     | R        | A |
| Strategy retired (qualitative decision)                        | R      | R        | A     | I        | I |
| Strategy revised (MAJOR bump → re-enter pipeline)              | R      | R        | A     | I        | C |
| Selection-layer human override                                 |        |          | I     | R        | I (audit) |
| LLM unavailable: fall back to previous cycle                   |        |          | I     | I        | R/A |
| LLM unavailable ≥ 3 cycles: DORMANT all                        |        |          | I     | C        | R/A |
| Global kill-switch trip                                        |        |          | I     | C        | R/A |
| Global kill-switch operator command                            |        |          | C     | R/A      | I |
| Global kill-switch clear (post-incident)                       |        | C        | A     | R        | I |
| Feature deprecated                                             | R      | R        | A     | I        | C |
| New auxiliary class registered                                 | R      | R        | A     | I        | C |

The `System` row is the implementation reality of automatic decisions;
it is bounded by code in this repo plus the contracts in `11` and
`12`.

---

## Evidence package standards

Each `A`-level decision requires a structured evidence package archived
in the audit trail. The required artifact set is decision-type-specific.

### Strategy promotion to `live`

Per `08_development_pipeline.md:213-228`. Required artifacts:

1. Mechanism document (Stage 2 output).
2. Pre-registered hypothesis (`06_hypothesis_system.md` output).
3. Spec YAML at the version being promoted, with all required
   `04_strategy_spec_schema.md` sections populated.
4. Sanity-check backtest report (Stage 4).
5. Walk-forward report (Stage 5; gate per
   `07_auto_generation.md:171`). Required fields: per-window stats,
   aggregate stats, gate verdict.
6. CPCV report (Stage 6; deferred per
   `05_backtest_harness.md:35`). When CPCV is shipped, required.
7. Stress-period replay report (Stage 6; deferred). When shipped,
   required.
8. Paper-trading record (Stage 7; min trade count per
   `08_development_pipeline.md:195, 203-208`).
9. Library-fit rationale (correlation with existing live strategies).
10. Retirement criteria authored as part of the spec
    (`08_development_pipeline.md:220`).
11. Tier decision (default `confirm_then_execute`).

### MAJOR-version revision

Same as promotion, but artifacts may be partial reruns where the change
is scoped:
- Mechanism doc unchanged: existing copy referenced; not regenerated.
- Spec at new version mandatory.
- Walk-forward / CPCV / stress / paper rerun mandatory regardless of
  scope (no shortcuts).
- Diff-against-previous-version section explaining what changed and
  why.

### MINOR / parameter change inside envelope

Lighter packet:
- Spec diff at the parameter changes.
- Sanity backtest demonstrating the new parameters land inside the
  validated envelope.
- Walk-forward not required if parameters fall within previously
  validated grid.
- `Author` self-certifies via the harness CLI; `System` validates.

### Risk-envelope expansion

- Backtest evidence at the proposed envelope.
- Stress-period evidence at the proposed envelope.
- Paper-trading replay sampled at the proposed envelope.
- Owner sign-off.

### Re-enable after auto-disable

- Root-cause analysis of the disable trigger.
- Fresh validation packet (walk-forward at minimum, CPCV when shipped,
  per `00_master_architecture.md:179`). The harness writes signed
  evidence packets to `data/evidence/` automatically; `tradegy
  validate-evidence <packet.json>` verifies the signature.
- For promotion to `live` tier the packet MUST be HMAC-signed
  (`TRADEGY_EVIDENCE_KEY` set when the harness ran). SHA256-only
  packets are rejected for governance-grade decisions because the
  digest is recomputable and not unforgeable; the packet itself
  carries a `warning` field that flags this. See
  `05_backtest_harness.md` § Evidence signing for the implementation.
- Operator sign-off that the underlying condition is resolved.
- Owner sign-off.

### Strategy retirement (qualitative)

- Mechanism re-examination summary.
- Live performance summary.
- Disposition of open positions and outstanding orders.
- Library-gap impact statement.

---

## Approval authority

| Decision class                       | Minimum approval                     |
|--------------------------------------|--------------------------------------|
| New live strategy                    | `Owner` + `Reviewer` (24-hour cool-off) |
| Tier graduation                      | `Owner` after declared minimum live trade count |
| MAJOR-version revision               | `Owner` + `Reviewer`                 |
| Risk-envelope expansion              | `Owner` + `Reviewer`                 |
| Risk-envelope reduction (urgent)     | `Owner` (single)                     |
| Re-enable after auto-disable         | `Owner` after operator sign-off      |
| Daily-cap modification               | `Owner` + `Reviewer`                 |
| Kill-switch clear                    | `Owner`                              |
| MINOR / in-envelope parameter change | `Author` self-cert with `System` validation |
| Auxiliary-class registration         | `Owner` (treated as code change)     |

### Quorum (multi-operator deployments)

When two or more humans are available:
- `Owner` and `Reviewer` MUST be different humans for any decision
  requiring both.
- Cool-off can shorten to 4 hours when independence is by-person.
- Single-human cool-off remains 24 hours when only one human is in
  rotation.

---

## Promotion checklist template

Stored at `governance/templates/promotion_packet.md` (to be created in
the repo when first used). Required fields:

```
# Promotion Packet — {strategy_id} v{version}

## Mechanism
- Document version: {path:line}
- Counterparty identified: yes/no
- Causal story summary: ...

## Validation evidence
- Walk-forward gate: PASS / FAIL — {summary}
- CPCV (when shipped): PASS / FAIL — {summary}
- Stress-period replay (when shipped): PASS / FAIL — {summary}
- Paper-trading: {trade_count}, Sharpe {paper}/{backtest}, divergence {pct}

## Library fit
- Correlations with existing live strategies: ...
- Net-margin contribution rationale: ...

## Retirement criteria
- Quantitative: ...
- Qualitative: ...

## Tier decision
- Initial tier: confirm_then_execute / proposal_only
- Graduation conditions: minimum N trades; envelope-consistent performance

## Sign-offs
- Author: {name, date}
- Reviewer: {name, date} — must differ from Author by at least 24h
- Owner: {name, date}

## Audit references
- Spec yaml hash: ...
- Evidence run ids: ...
- Kill criteria for v1: ...
```

---

## Revision checklist template

Stored at `governance/templates/revision_packet.md`. Required fields:

```
# Revision Packet — {strategy_id} v{old} → v{new}

## Scope
- MAJOR / MINOR
- Diff summary: ...

## Triggering reason
- Drift / envelope drift / better variant / market structure / other

## What changed
- Mechanism: yes/no
- Spec fields: ...
- Parameters: ...
- Risk envelope: ...

## Re-validation
- Walk-forward: PASS/FAIL
- CPCV (when shipped): PASS/FAIL
- Stress (when shipped): PASS/FAIL
- Paper rerun: {as applicable}

## Old version disposition
- Status: retired / superseded
- Open positions: closed / migrated / N/A

## Sign-offs
- Author / Reviewer / Owner with cool-off compliance
```

---

## Retirement decision criteria

Aligned with `08_development_pipeline.md:256-279`. Concrete thresholds:

### Quantitative auto-retire (System triggered → Owner I)

- Spec's `retirement_criteria.quantitative_triggers` fired.
- Three CRITICAL monitoring alerts attributable to the strategy in 10
  consecutive sessions.
- Per-strategy 10-trade rolling realized-R below the spec's declared
  envelope floor.
- Realized Sharpe < 0 over the spec's declared evaluation horizon.

Action: strategy auto-disabled per `12_live_monitoring_spec.md`. Owner
informed. Re-enable requires fresh validation packet.

### Qualitative retirement (Owner triggered)

- Mechanism re-examination concludes the structural counterparty no
  longer exists.
- Library-correlation analysis shows the strategy is now redundant.
- Author's mechanism doc references a market regime that no longer
  applies.

Action: scheduled retirement at the next session boundary. Open
positions flatten under `ExitReason.OVERRIDE`.

### Soft-threshold escalation

`12_live_monitoring_spec.md` WARNING-level alerts accumulate. Owner
reviews weekly. Two consecutive weeks of soft alerts trigger a Stage-9
revalidation per `08_development_pipeline.md:244` — not retirement, but
not silent acceptance.

---

## Exception handling

### Fast-track risk reduction

For urgent capital protection (broker outage, unusual market event,
data-feed failure detected mid-session):

- `Owner` MAY directly trigger global kill-switch via the operator CLI.
- `Owner` MAY directly reduce a per-strategy cap.
- `Owner` MAY directly retire a strategy (no MAJOR bump required).
- These actions bypass the cool-off requirement.
- Audit-log entry MUST be filed within 1 hour of the action.

### Approval TTL

A signed-off promotion or revision packet expires if the strategy is
not deployed within 14 days. Re-deployment after expiry requires fresh
sanity backtest at minimum.

### Rollback authority

`Owner` MAY revert a tier graduation, parameter change, or risk-
envelope expansion at any time without `Reviewer` involvement. Rollback
is logged but does not require a separate evidence packet.

`Owner` MAY NOT unilaterally promote: rollback removes safety; promotion
adds risk.

---

## Audit-trail requirements

### Format

JSONL append-only files at `audit/`:

```
audit/
  promotions.jsonl       # one line per promotion / revision / retirement decision
  parameter_changes.jsonl # one line per in-envelope change
  envelope_changes.jsonl  # one line per envelope expansion/reduction
  selection_overrides.jsonl # one line per human override of LLM selection
  kill_switch.jsonl       # one line per trip and clear
  alerts.jsonl            # all raised alerts (mirrored from monitoring)
  re_enables.jsonl        # one line per auto-disable re-enable
```

### Required fields per record

```
{
  "ts_utc": "...",
  "decision": "promote_strategy",
  "subject": "mes_vwap_reversion",
  "version": "1.0.0",
  "actor": "owner_alice",
  "evidence_packet": "audit/packets/2026-04-30T10:15:00Z_mes_vwap_reversion_promote.json",
  "sign_offs": [{"role": "author", "name": "...", "ts": "..."}, ...],
  "rationale": "...",
  "linked_run_ids": ["..."]
}
```

### Retention

- Promotion / revision / retirement: retained indefinitely.
- Parameter changes: retained for the strategy's lifetime + 7 years.
- Selection overrides and alerts: 7 years.
- Kill-switch records: indefinitely.

### Tamper-evidence

Append-only by convention (no in-place edits, no deletions). Hash chain
optional for v1; required before scaling beyond single-operator. The
hash-chain decision is deferred to a follow-on infrastructure spec.

---

## Post-session review feedback loop

Per `09_selection_layer.md:274` and
`08_development_pipeline.md:333`. Daily after session close:

1. `Operator` reviews monitoring report from `12_live_monitoring_spec.md`.
2. `Operator` reviews selection-layer audit log.
3. Patterns surfaced (envelope breaches, soft alerts, override
   clusters, replay divergence) feed into:
   - Hypothesis-system observations queue (per
     `06_hypothesis_system.md`).
   - Weekly Owner review.
4. Weekly: `Owner` reviews accumulated observations; decides whether
   any warrant Stage-9 revalidation, MAJOR revision, or retirement.

Post-session review is a `R/A` for `Operator` daily and `R/A` for
`Owner` weekly.

---

## Cross-references

- `00_master_architecture.md:169-198` — governance authority table and
  tier model.
- `04_strategy_spec_schema.md:80-94, 391` — schema mutability and audit
  log requirement.
- `06_hypothesis_system.md` — feedback loop sink.
- `07_auto_generation.md:171` — walk-forward gate authority.
- `08_development_pipeline.md:213-228, 256-279, 316-333` — promotion
  packet, retirement, human touchpoints.
- `09_selection_layer.md:212, 318, 343` — overrides, auto-disable.
- `11_execution_layer_spec.md` — kill-switch trigger authority.
- `12_live_monitoring_spec.md` — alert severity and re-enable surface.

---

## Resolved decisions

These were open at draft time and are now resolved. Each carries a
short rationale; the chosen behavior is binding for v1.

1. **Reviewer independence in single-operator v1 — cool-off plus tier
   ceiling.** The 24-hour cool-off plus a written self-review template
   (stored at `governance/templates/self_review.md` when first used)
   IS sufficient procedural integrity for promotion to
   `proposal_only` and `confirm_then_execute`. **Promotion to
   `auto_execute` is blocked entirely until a second human is
   available** as `Reviewer`. This is a hard ceiling, enforced by the
   tier-graduation check in this doc's RACI matrix.
2. **Audit-log hash chain — deferred to multi-operator.** Append-only
   by convention is sufficient for v1. The hash chain becomes a hard
   prerequisite for the second-operator transition (tracked in this
   doc's "Multi-operator transition" entry).
3. **Approval-packet TTL — 14 days for promotion, 30 days for
   revision.** Promotion expiry is conservative (the strategy hasn't
   been live yet). Revision expiry is longer because revision packets
   are scoped to specific changes that don't decay as quickly as
   "fresh data" assumptions.
4. **Auto-graduation discipline — N AND time-in-tier.** Graduation
   from `confirm_then_execute` to `auto_execute` requires **both**
   minimum 30 live trades AND minimum 30 days time-in-tier. Either
   alone is gameable — N alone allows fast-trading strategies to
   graduate before market regimes shift; time alone allows
   low-activity strategies to graduate without enough evidence.
5. **Evidence-packet schema — versioned and validated.** A new CLI
   subcommand `tradegy validate-evidence` mirrors the spec-validator
   pattern. Schema lives alongside the spec schema. Required before
   any promotion review accepts the packet.
6. **Selection-override budget — soft, alert at >20%.** No hard
   block. The system tracks per-operator, per-session override rate
   and emits a WARNING (per `12_live_monitoring_spec.md`) when it
   exceeds 20%. Operators sometimes legitimately need to override
   beyond 20% (rare events, system catching up to context); a hard
   cap would force the wrong tradeoff.
7. **Retirement reversal — re-validated retired strategies count as
   new.** Kill-rate accounting (per
   `08_development_pipeline.md:283-296`) treats them as fresh entries
   to the pipeline. The retired version stays in the historical
   record at its retired status.
8. **Multi-operator transition — deferred to a dedicated mini-spec.**
   Tracked here as a known prerequisite. Hard prerequisites for that
   transition: audit-log hash chain (this doc, item 2), PagerDuty-
   equivalent on-call rotation (`12_live_monitoring_spec.md`,
   resolved item 1), `client_order_id` namespace partition documented
   in `11_execution_layer_spec.md`, and the `auto_execute` tier
   ceiling lifted (this doc, item 1).

## Still open

- The multi-operator transition mini-spec itself. Drafted when a
  second operator is on the horizon.
- LLM cost-budget governance — flagged as P2 in
  `10_review_gap_analysis.md:102-106`. Out of scope for this doc;
  needs its own treatment.
