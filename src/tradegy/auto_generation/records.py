"""VariantRecord schema + append-only JSONL persistence.

Per `07_auto_generation.md` § Variant tracking (§185-214). Every
generated variant gets logged regardless of outcome — passed,
discarded at sanity, killed at walk-forward, etc. The log is the
audit trail "for hypothesis X, how many variants did we test, what
did we find, what did we pick."

Storage: `data/auto_generation/<hypothesis_id>/variants.jsonl` —
append-only, one JSON record per line. Ordering preserved within a
single process; multi-process is out of scope (single-operator v1
aligns with `13_governance_process.md`).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from tradegy import config


class GateOutcome(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    NOT_RUN = "not_run"


class VariantOutcome(str, Enum):
    """Per doc 07 §211: where the variant ended up.

    `discarded_at_*` corresponds to the gate that killed it; `passed`
    means it cleared every gate and is in the candidate pool;
    `validation_failed` means it failed pre-backtest validation
    (schema, registry, envelope, diversity).
    """

    VALIDATION_FAILED = "validation_failed"
    DISCARDED_AT_SANITY = "discarded_at_sanity"
    DISCARDED_AT_WALK_FORWARD = "discarded_at_walk_forward"
    DISCARDED_AT_HOLDOUT = "discarded_at_holdout"
    PASSED = "passed"
    ERROR = "error"


@dataclass(frozen=True)
class VariantStats:
    """Aggregate stats persisted with the variant for audit. Doc 07
    §205-209 lists raw_sharpe / deflated_sharpe / corrected_threshold;
    we additionally carry per-gate Sharpes when each ran.
    """

    raw_sharpe: float | None = None
    deflated_sharpe: float | None = None
    corrected_threshold: float | None = None
    sanity_sharpe: float | None = None
    walk_forward_avg_oos_sharpe: float | None = None
    walk_forward_avg_in_sample_sharpe: float | None = None
    holdout_sharpe: float | None = None
    total_trades: int | None = None


@dataclass(frozen=True)
class GateResults:
    """Per-gate outcome map for one variant."""

    sanity: GateOutcome = GateOutcome.NOT_RUN
    walk_forward: GateOutcome = GateOutcome.NOT_RUN
    holdout: GateOutcome = GateOutcome.NOT_RUN


@dataclass(frozen=True)
class VariantRecord:
    """One row of the per-hypothesis variants log."""

    variant_id: str
    hypothesis_id: str
    generated_at: str  # ISO 8601
    generator_id: str  # e.g. "anthropic_variant_generator_v1" | "stub"
    generator_metadata: dict[str, Any]
    spec_id: str
    spec_hash: str
    spec_version: str
    budget_used: int
    budget_cap: int
    gate_results: GateResults
    stats: VariantStats
    outcome: VariantOutcome
    fail_reason: str = ""
    sibling_variant_ids: tuple[str, ...] = ()


def variant_log_path(hypothesis_id: str, *, root: Path | None = None) -> Path:
    """Where the JSONL log for `hypothesis_id` lives."""
    base = root or config.data_dir() / "auto_generation"
    return base / hypothesis_id / "variants.jsonl"


def _record_to_dict(rec: VariantRecord) -> dict:
    d = asdict(rec)
    d["gate_results"] = {
        k: v.value if isinstance(v, GateOutcome) else v
        for k, v in d["gate_results"].items()
    }
    d["outcome"] = rec.outcome.value
    return d


def _record_from_dict(d: dict) -> VariantRecord:
    gr = d.get("gate_results", {})
    return VariantRecord(
        variant_id=d["variant_id"],
        hypothesis_id=d["hypothesis_id"],
        generated_at=d["generated_at"],
        generator_id=d["generator_id"],
        generator_metadata=dict(d.get("generator_metadata", {})),
        spec_id=d["spec_id"],
        spec_hash=d["spec_hash"],
        spec_version=d["spec_version"],
        budget_used=int(d["budget_used"]),
        budget_cap=int(d["budget_cap"]),
        gate_results=GateResults(
            sanity=GateOutcome(gr.get("sanity", "not_run")),
            walk_forward=GateOutcome(gr.get("walk_forward", "not_run")),
            holdout=GateOutcome(gr.get("holdout", "not_run")),
        ),
        stats=VariantStats(**d.get("stats", {})),
        outcome=VariantOutcome(d["outcome"]),
        fail_reason=d.get("fail_reason", ""),
        sibling_variant_ids=tuple(d.get("sibling_variant_ids", [])),
    )


def append_record(record: VariantRecord, *, root: Path | None = None) -> Path:
    """Append one record to the per-hypothesis JSONL log. Creates the
    parent dir on demand. Returns the path written to.
    """
    path = variant_log_path(record.hypothesis_id, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(_record_to_dict(record), separators=(",", ":"))
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return path


def read_records(
    hypothesis_id: str, *, root: Path | None = None
) -> list[VariantRecord]:
    """Read every record for `hypothesis_id`. Returns empty list if
    the log doesn't exist yet.
    """
    path = variant_log_path(hypothesis_id, root=root)
    if not path.exists():
        return []
    out: list[VariantRecord] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            out.append(_record_from_dict(json.loads(line)))
    return out


def now_utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
