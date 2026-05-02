"""AutoTestOrchestrator — runs N variants of one hypothesis through
the harness gates and persists structured records.

Per `07_auto_generation.md` §161-179. The orchestrator owns the
sanity → walk-forward → holdout sequence, applies the multi-
hypothesis correction across the variant pool, and writes one
VariantRecord per variant regardless of outcome (passed, killed at
sanity, validation_failed, error).

The Deflated Sharpe approximation used here is the Bonferroni-style
fallback per doc 07 §111-113. Full DSR (López de Prado) is left as
an upgrade path — the structure is the same; only the threshold
formula changes.

Storage:
  data/auto_generation/<hyp_id>/variants.jsonl      (this module)
  data/evidence/<spec_id>__*.json                    (existing harness)
"""
from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from dateutil.relativedelta import relativedelta

from tradegy.auto_generation.generators import GenerationContext, VariantGenerator
from tradegy.auto_generation.hypothesis import Hypothesis
from tradegy.auto_generation.records import (
    GateOutcome,
    GateResults,
    VariantOutcome,
    VariantRecord,
    VariantStats,
    append_record,
    now_utc_iso,
    read_records,
)
from tradegy.harness import (
    CostModel,
    WalkForwardConfig,
    run_backtest,
    run_walk_forward,
)
from tradegy.harness.data import load_bar_stream
from tradegy.specs.loader import SpecValidationError, validate_spec
from tradegy.specs.schema import StrategySpec


_log = logging.getLogger(__name__)


@dataclass
class AutoTestSummary:
    """Aggregate verdict for one hypothesis run."""

    hypothesis_id: str
    variants_generated: int
    variants_validation_failed: int
    variants_passed_sanity: int
    variants_passed_walk_forward: int
    variants_passed_holdout: int
    candidate_pool_ids: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    @property
    def candidate_count(self) -> int:
        return len(self.candidate_pool_ids)


# ─── Multi-hypothesis correction ──────────────────────────────


def _corrected_sharpe_lift(*, n_variants: int, total_trades: int) -> float:
    """Bonferroni-flavored lift for the Sharpe gate threshold.

    Corrects the per-variant gate threshold for the multiple-testing
    inflation when N variants are tested and the best is selected.
    Approximate formula:

        lift = sqrt(2 * ln(N) / T)

    where T is the trade count of the variant being evaluated. This
    is the simpler fallback per doc 07 §111-113. The "preferred"
    Deflated Sharpe Ratio (López de Prado) requires the variant
    Sharpe correlation matrix and is left as future work; the data
    shape (variants_pool below) supports plugging it in without
    changing the orchestrator's flow.
    """
    if n_variants <= 1 or total_trades <= 0:
        return 0.0
    return math.sqrt(2.0 * math.log(n_variants) / total_trades)


# ─── Diversity check (MVP: spec-content hash dedup) ────────────


def _spec_content_hash(spec: StrategySpec) -> str:
    payload = spec.model_dump_json().encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ─── Orchestrator ─────────────────────────────────────────────


class AutoTestOrchestrator:
    """Runs one hypothesis's variant pool through the gates.

    Construction takes:
      * the hypothesis (drives budget + gates + parameter envelope)
      * a VariantGenerator (stub or LLM-backed)
      * a GenerationContext (registry snapshot)
      * an optional cost model (default: harness defaults)
      * an optional persist_root (test override for the JSONL path)

    Call `run()` to execute the full pipeline. `run()` returns an
    AutoTestSummary; per-variant detail is in the JSONL log.
    """

    def __init__(
        self,
        *,
        hypothesis: Hypothesis,
        variant_generator: VariantGenerator,
        context: GenerationContext,
        cost: CostModel | None = None,
        persist_root: Path | None = None,
        run_walk_forward_on_pass: bool = True,
        run_holdout_on_pass: bool = False,
        holdout_months: int = 0,
    ) -> None:
        if hypothesis.status != "promoted":
            raise ValueError(
                f"AutoTestOrchestrator: hypothesis {hypothesis.id!r} is "
                f"{hypothesis.status!r}, must be 'promoted' before testing"
            )
        self._h = hypothesis
        self._variant_gen = variant_generator
        self._ctx = context
        self._cost = cost or CostModel()
        self._persist_root = persist_root
        self._run_wf = run_walk_forward_on_pass
        self._run_holdout = run_holdout_on_pass
        self._holdout_months = holdout_months

        # Enforce budget pre-registration: count any variants already
        # logged for this hypothesis. Doc 07 §218-228 forbids re-running
        # holdout or expanding the variant budget post-hoc.
        self._already_logged = read_records(
            hypothesis.id, root=self._effective_root_for_records()
        )
        if len(self._already_logged) >= hypothesis.variant_budget:
            raise ValueError(
                f"hypothesis {hypothesis.id!r} already has "
                f"{len(self._already_logged)} variants logged "
                f"(budget cap {hypothesis.variant_budget}); "
                "expanding the budget post-hoc is forbidden per "
                "07_auto_generation.md:218-228"
            )

    # ── Public entry point ────────────────────────────────────

    def run(self) -> AutoTestSummary:
        summary = AutoTestSummary(
            hypothesis_id=self._h.id,
            variants_generated=0,
            variants_validation_failed=0,
            variants_passed_sanity=0,
            variants_passed_walk_forward=0,
            variants_passed_holdout=0,
            started_at=now_utc_iso(),
        )

        remaining_budget = self._h.variant_budget - len(self._already_logged)
        variants = self._variant_gen.generate(
            hypothesis=self._h, context=self._ctx, n=remaining_budget,
        )
        summary.variants_generated = len(variants)

        # Pre-backtest validation + dedup.
        validated, failed = self._validate_and_dedup(variants)
        summary.variants_validation_failed = len(failed)
        for spec, reason in failed:
            self._log_failure(spec, reason, summary, VariantOutcome.VALIDATION_FAILED)

        # Run gates on the survivors. Compute the corrected-threshold
        # lift after we know how many variants are actually testable.
        n_tested = len(validated)
        sibling_ids = tuple(s.metadata.id for s in validated)

        for spec in validated:
            self._run_one_variant(
                spec=spec,
                n_pool=n_tested,
                sibling_ids=sibling_ids,
                summary=summary,
            )

        summary.finished_at = now_utc_iso()
        return summary

    # ── Internal helpers ──────────────────────────────────────

    def _effective_root_for_records(self) -> Path | None:
        return self._persist_root

    def _coverage_for(self, spec: StrategySpec) -> tuple[datetime, datetime]:
        """Resolve [first_bar_ts, last_bar_ts] for the spec's instrument.

        The orchestrator always runs against the bar feature's full
        coverage; holdout reservation is computed off this span.
        """
        bars = load_bar_stream(spec.market_scope.instrument)
        cs = bars.row(0, named=True)["ts_utc"]
        ce = bars.row(-1, named=True)["ts_utc"]
        return cs, ce

    def _validate_and_dedup(
        self, variants: list[StrategySpec]
    ) -> tuple[list[StrategySpec], list[tuple[StrategySpec, str]]]:
        """Apply schema + registry + envelope validation per
        spec.loader.validate_spec, plus a content-hash dedup.
        """
        validated: list[StrategySpec] = []
        failed: list[tuple[StrategySpec, str]] = []
        seen_hashes: set[str] = set()

        for spec in variants:
            errs = validate_spec(spec)
            if errs:
                failed.append((spec, "validation_errors:" + ";".join(errs)))
                continue
            h = _spec_content_hash(spec)
            if h in seen_hashes:
                failed.append((spec, "duplicate_content_hash"))
                continue
            seen_hashes.add(h)
            validated.append(spec)
        return validated, failed

    def _run_one_variant(
        self,
        *,
        spec: StrategySpec,
        n_pool: int,
        sibling_ids: tuple[str, ...],
        summary: AutoTestSummary,
    ) -> None:
        """Run sanity → walk-forward → holdout for one variant. Each
        gate failure short-circuits and persists the appropriate
        VariantRecord.
        """
        thresholds = self._h.gate_thresholds
        gate_results = GateResults()
        stats = VariantStats()

        # Stage 4: sanity backtest.
        try:
            bt = run_backtest(spec, cost=self._cost)
        except Exception as exc:  # noqa: BLE001
            self._persist(
                spec=spec, sibling_ids=sibling_ids, summary=summary,
                outcome=VariantOutcome.ERROR,
                gate_results=gate_results, stats=stats,
                fail_reason=f"sanity_backtest_raised:{exc!r}",
            )
            return

        sanity_sharpe = bt.stats.sharpe if bt.stats else 0.0
        sanity_trades = bt.stats.total_trades if bt.stats else 0
        stats = VariantStats(
            sanity_sharpe=sanity_sharpe, total_trades=sanity_trades,
            raw_sharpe=sanity_sharpe,
        )
        sanity_passed = (
            sanity_trades >= thresholds.sanity_min_trades
            and sanity_sharpe > thresholds.sanity_min_in_sample_sharpe
        )
        if not sanity_passed:
            gate_results = GateResults(sanity=GateOutcome.FAILED)
            self._persist(
                spec=spec, sibling_ids=sibling_ids, summary=summary,
                outcome=VariantOutcome.DISCARDED_AT_SANITY,
                gate_results=gate_results, stats=stats,
                fail_reason=(
                    f"sanity: trades={sanity_trades} (min "
                    f"{thresholds.sanity_min_trades}), "
                    f"sharpe={sanity_sharpe:+.3f} (>"
                    f" {thresholds.sanity_min_in_sample_sharpe})"
                ),
            )
            return

        gate_results = GateResults(sanity=GateOutcome.PASSED)
        summary.variants_passed_sanity += 1

        # Stage 5: walk-forward (with multi-hypothesis correction).
        if not self._run_wf:
            self._persist(
                spec=spec, sibling_ids=sibling_ids, summary=summary,
                outcome=VariantOutcome.PASSED,
                gate_results=gate_results, stats=stats, fail_reason="",
            )
            summary.candidate_pool_ids.append(spec.metadata.id)
            return

        try:
            cs, ce = self._coverage_for(spec)
            # Reserve the trailing N months as holdout; walk-forward
            # only sees [cs, ce - N months) so the holdout truly is
            # untouched by every fold of the gate.
            wf_end = ce
            if self._run_holdout and self._holdout_months > 0:
                wf_end = ce - relativedelta(months=self._holdout_months)
            wf = run_walk_forward(
                spec,
                coverage_start=cs,
                coverage_end=wf_end,
                config=WalkForwardConfig(
                    train_years=3.0, test_years=1.0, roll_years=1.0,
                ),
                cost=self._cost,
            )
        except Exception as exc:  # noqa: BLE001
            self._persist(
                spec=spec, sibling_ids=sibling_ids, summary=summary,
                outcome=VariantOutcome.ERROR,
                gate_results=gate_results, stats=stats,
                fail_reason=f"walk_forward_raised:{exc!r}",
            )
            return

        lift = _corrected_sharpe_lift(
            n_variants=n_pool, total_trades=sanity_trades,
        )
        corrected_threshold = (
            thresholds.walk_forward_min_in_sample_sharpe + lift
        )
        stats = VariantStats(
            sanity_sharpe=sanity_sharpe,
            total_trades=sanity_trades,
            raw_sharpe=sanity_sharpe,
            walk_forward_avg_oos_sharpe=wf.avg_oos_sharpe,
            walk_forward_avg_in_sample_sharpe=wf.avg_in_sample_sharpe,
            deflated_sharpe=wf.avg_oos_sharpe - lift,
            corrected_threshold=corrected_threshold,
        )

        wf_passed = (
            wf.passed
            and wf.avg_in_sample_sharpe >= corrected_threshold
        )
        if not wf_passed:
            gate_results = GateResults(
                sanity=GateOutcome.PASSED, walk_forward=GateOutcome.FAILED,
            )
            reason = wf.fail_reason or (
                f"corrected: IS Sharpe {wf.avg_in_sample_sharpe:+.3f} < "
                f"corrected threshold {corrected_threshold:+.3f} "
                f"(lift={lift:+.3f}, N={n_pool})"
            )
            self._persist(
                spec=spec, sibling_ids=sibling_ids, summary=summary,
                outcome=VariantOutcome.DISCARDED_AT_WALK_FORWARD,
                gate_results=gate_results, stats=stats,
                fail_reason=reason,
            )
            return

        gate_results = GateResults(
            sanity=GateOutcome.PASSED, walk_forward=GateOutcome.PASSED,
        )
        summary.variants_passed_walk_forward += 1

        # Stage 6 (optional in this pass): holdout.
        if not self._run_holdout or self._holdout_months <= 0:
            self._persist(
                spec=spec, sibling_ids=sibling_ids, summary=summary,
                outcome=VariantOutcome.PASSED,
                gate_results=gate_results, stats=stats, fail_reason="",
            )
            summary.candidate_pool_ids.append(spec.metadata.id)
            return

        # The trailing-months window was reserved out of walk-forward
        # above (wf saw [cs, ce - holdout_months)); now run a single
        # backtest on the held-out tail and gate it at 0.5x walk-
        # forward avg OOS Sharpe, mirroring cli._evaluate_holdout.
        # 07_auto_generation.md:165 specifies the gate formula.
        holdout_end = ce
        holdout_start = ce - relativedelta(months=self._holdout_months)
        try:
            ho = run_backtest(
                spec, start=holdout_start, end=holdout_end, cost=self._cost,
            )
        except Exception as exc:  # noqa: BLE001
            self._persist(
                spec=spec, sibling_ids=sibling_ids, summary=summary,
                outcome=VariantOutcome.ERROR,
                gate_results=GateResults(
                    sanity=GateOutcome.PASSED,
                    walk_forward=GateOutcome.PASSED,
                ),
                stats=stats,
                fail_reason=f"holdout_backtest_raised:{exc!r}",
            )
            return

        holdout_sharpe = ho.stats.sharpe if ho.stats else 0.0
        ratio = self._h.gate_thresholds.holdout_sharpe_ratio_to_walk_forward
        threshold = ratio * wf.avg_oos_sharpe
        # Reference Sharpe must be positive for the holdout gate to mean
        # anything — a negative wf.avg_oos_sharpe would produce a
        # negative threshold any holdout could clear, which is the
        # opposite of the gate's intent.
        if wf.avg_oos_sharpe <= 0:
            holdout_passed = False
            ho_reason = (
                f"reference walk-forward avg OOS sharpe "
                f"{wf.avg_oos_sharpe:+.3f} ≤ 0; holdout gate cannot pass "
                "without prior-stage edge"
            )
        else:
            holdout_passed = holdout_sharpe >= threshold
            ho_reason = (
                f"holdout sharpe {holdout_sharpe:+.3f} "
                f"{'≥' if holdout_passed else '<'} {ratio:g} × "
                f"wf avg OOS ({wf.avg_oos_sharpe:+.3f}) = "
                f"{threshold:+.3f}"
            )

        stats = VariantStats(
            sanity_sharpe=stats.sanity_sharpe,
            total_trades=stats.total_trades,
            raw_sharpe=stats.raw_sharpe,
            walk_forward_avg_oos_sharpe=stats.walk_forward_avg_oos_sharpe,
            walk_forward_avg_in_sample_sharpe=stats.walk_forward_avg_in_sample_sharpe,
            deflated_sharpe=stats.deflated_sharpe,
            corrected_threshold=stats.corrected_threshold,
            holdout_sharpe=holdout_sharpe,
        )

        if not holdout_passed:
            self._persist(
                spec=spec, sibling_ids=sibling_ids, summary=summary,
                outcome=VariantOutcome.DISCARDED_AT_HOLDOUT,
                gate_results=GateResults(
                    sanity=GateOutcome.PASSED,
                    walk_forward=GateOutcome.PASSED,
                    holdout=GateOutcome.FAILED,
                ),
                stats=stats,
                fail_reason=ho_reason,
            )
            return

        self._persist(
            spec=spec, sibling_ids=sibling_ids, summary=summary,
            outcome=VariantOutcome.PASSED,
            gate_results=GateResults(
                sanity=GateOutcome.PASSED,
                walk_forward=GateOutcome.PASSED,
                holdout=GateOutcome.PASSED,
            ),
            stats=stats,
            fail_reason="",
        )
        summary.variants_passed_holdout += 1
        summary.candidate_pool_ids.append(spec.metadata.id)

    def _persist(
        self,
        *,
        spec: StrategySpec,
        sibling_ids: tuple[str, ...],
        summary: AutoTestSummary,
        outcome: VariantOutcome,
        gate_results: GateResults,
        stats: VariantStats,
        fail_reason: str,
    ) -> None:
        record = VariantRecord(
            variant_id=spec.metadata.id,
            hypothesis_id=self._h.id,
            generated_at=now_utc_iso(),
            generator_id=self._variant_gen.id,
            generator_metadata={"version": "1"},
            spec_id=spec.metadata.id,
            spec_hash=_spec_content_hash(spec),
            spec_version=spec.metadata.version,
            budget_used=summary.variants_generated,
            budget_cap=self._h.variant_budget,
            gate_results=gate_results,
            stats=stats,
            outcome=outcome,
            fail_reason=fail_reason,
            sibling_variant_ids=sibling_ids,
        )
        append_record(record, root=self._persist_root)

    def _log_failure(
        self,
        spec: StrategySpec,
        reason: str,
        summary: AutoTestSummary,
        outcome: VariantOutcome,
    ) -> None:
        self._persist(
            spec=spec,
            sibling_ids=tuple(),
            summary=summary,
            outcome=outcome,
            gate_results=GateResults(),
            stats=VariantStats(),
            fail_reason=reason,
        )
