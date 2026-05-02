"""Killed-hypothesis summaries for the auto-generator's prompt context.

The hypothesis prompt today says "avoid mechanistic near-duplicates of
failed prior hypotheses" abstractly — the LLM has no idea which
hypotheses actually failed. This module loads the on-disk hypothesis
ledger plus the per-hypothesis variant logs, derives a "killed"
classification, and renders a tight summary block the generator can
inject as a stable context section.

A hypothesis counts as killed when any of:

  * its YAML carries `status: killed` (explicit operator decision);
  * its YAML carries `status: retired` (graduated then retired);
  * it has at least one VariantRecord on disk and **every** record is
    a discard / validation-failure / error (i.e. the budget was used
    and nothing survived).

The third rule is the load-bearing one — most hypotheses don't get
their `status` field flipped to `killed` by hand. The variant log is
the authoritative outcome of the auto-test orchestrator and is
trustworthy.

Output rendering keeps each entry to one short paragraph: title,
mechanism (truncated), and a one-line "what we tried, what failed".
The point is to anchor the LLM in concrete prior failures without
flooding the prompt — keep this block under ~2k tokens even for many
killed hypotheses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from tradegy.auto_generation.hypothesis import (
    Hypothesis,
    list_hypotheses,
)
from tradegy.auto_generation.records import (
    VariantOutcome,
    VariantRecord,
    read_records,
)


# Outcomes that count as "this variant didn't survive". `PASSED` is
# obviously survival; everything else means the variant was thrown
# away at some gate or never ran.
_DISCARD_OUTCOMES: frozenset[VariantOutcome] = frozenset({
    VariantOutcome.VALIDATION_FAILED,
    VariantOutcome.DISCARDED_AT_SANITY,
    VariantOutcome.DISCARDED_AT_WALK_FORWARD,
    VariantOutcome.DISCARDED_AT_HOLDOUT,
    VariantOutcome.ERROR,
})


@dataclass(frozen=True)
class KilledHypothesisSummary:
    """One row in the killed-hypothesis context block.

    `derived_kill_reason` is what the LLM sees as the failure mode.
    When the YAML's `kill_reason` field is set we use it verbatim;
    otherwise we synthesize one from the variant log (which gate
    most variants died at and what the typical fail_reason text was).
    """

    hypothesis_id: str
    title: str
    mechanism: str
    derived_kill_reason: str
    n_variants_recorded: int
    status: str  # the on-disk status field, for audit


def _summarize_variants(records: list[VariantRecord]) -> str:
    """Synthesize a one-line kill reason from the variant log.

    Picks the dominant outcome bucket and reports the modal
    fail_reason text, so the LLM sees the concrete failure mode (e.g.
    "all 9 variants discarded at sanity (typical: trades=0)") instead
    of just "killed".
    """
    if not records:
        return "no variant records on disk"
    bucket_counts: dict[str, int] = {}
    for r in records:
        bucket_counts[r.outcome.value] = bucket_counts.get(r.outcome.value, 0) + 1
    dominant_bucket, dominant_n = max(bucket_counts.items(), key=lambda kv: kv[1])
    dominant_reasons = [
        r.fail_reason for r in records
        if r.outcome.value == dominant_bucket and r.fail_reason
    ]
    sample = dominant_reasons[0] if dominant_reasons else ""
    parts = [
        f"{dominant_n}/{len(records)} variants {dominant_bucket}",
    ]
    if sample:
        parts.append(f"typical: {sample}")
    return "; ".join(parts)


def _is_killed(hyp: Hypothesis, records: list[VariantRecord]) -> bool:
    """Decide whether a hypothesis counts as killed for prompt
    injection. See module docstring for the rule.
    """
    if hyp.status in ("killed", "retired"):
        return True
    if records and all(r.outcome in _DISCARD_OUTCOMES for r in records):
        return True
    return False


def _truncate(s: str, max_len: int = 240) -> str:
    s = s.strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"


def load_kill_summaries(
    *,
    hypotheses_root: Path | None = None,
    variants_root: Path | None = None,
) -> list[KilledHypothesisSummary]:
    """Walk every hypothesis on disk, classify killed-or-not, and
    return a summary list (most-recent-first by created_date).

    Both roots are optional and resolved via `config` defaults when
    not passed; tests inject `tmp_path`-rooted dirs.
    """
    out: list[KilledHypothesisSummary] = []
    for hyp in list_hypotheses(root=hypotheses_root):
        records = read_records(hyp.id, root=variants_root)
        if not _is_killed(hyp, records):
            continue
        if hyp.kill_reason:
            reason = hyp.kill_reason.strip()
        else:
            reason = _summarize_variants(records)
        out.append(KilledHypothesisSummary(
            hypothesis_id=hyp.id,
            title=hyp.title,
            mechanism=_truncate(hyp.mechanism),
            derived_kill_reason=reason,
            n_variants_recorded=len(records),
            status=hyp.status,
        ))
    out.sort(key=lambda s: s.hypothesis_id, reverse=True)
    return out


def format_kill_summaries(
    summaries: Iterable[KilledHypothesisSummary],
    *,
    max_entries: int = 25,
) -> str:
    """Render the killed-hypothesis context block for the LLM prompt.

    Returns "" when nothing has been killed yet (so the caller can
    skip the section entirely). Caps at `max_entries` to keep the
    block under a reasonable token budget — the most recent kills
    are most informative.
    """
    items = list(summaries)
    if not items:
        return ""
    items = items[:max_entries]
    lines = [
        "## Recent failed hypotheses (do not propose mechanistic near-duplicates)",
        "",
        "Each entry below was generated, tested, and killed. Do not propose a "
        "hypothesis whose mechanism reduces to one of these, even with renamed "
        "features or rotated thresholds. The selection bottleneck is novelty of "
        "*mechanism*, not novelty of parameters.",
        "",
    ]
    for s in items:
        lines.append(f"- **{s.title}**")
        if s.mechanism:
            lines.append(f"  mechanism: {s.mechanism}")
        lines.append(f"  outcome: {s.derived_kill_reason}")
    if len(list(summaries)) > max_entries:
        lines.append(
            f"\n  (showing {max_entries} most-recent of "
            f"{len(list(summaries))} total killed hypotheses)"
        )
    return "\n".join(lines) + "\n"
