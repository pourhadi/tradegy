"""Generator interfaces + stub implementations for tests.

Two ABCs:

  * `HypothesisGenerator` — produces fresh Hypothesis records from
    a structured prompt context. Per `06_hypothesis_system.md`
    Stage 1 (ingestion) + Stage 2 (enrichment): the generator can
    seed from literature / market structure / human submission.
    The stub returns canned hypotheses for tests.

  * `VariantGenerator` — given one promoted Hypothesis, produces N
    StrategySpec drafts that mechanically express it using only
    registered classes/features per doc 07 §50-63. The stub returns
    canned specs for tests.

Real LLM-backed implementations (using the Anthropic SDK) live in
sibling modules (`anthropic_hypothesis_generator.py`,
`anthropic_variant_generator.py`) and import this ABC. Tests use
the stubs to drive the orchestrator without spending API tokens.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from tradegy.auto_generation.hypothesis import Hypothesis
from tradegy.specs.schema import StrategySpec

if TYPE_CHECKING:
    from tradegy.auto_generation.feature_stats import FeatureStats
    from tradegy.auto_generation.kill_log import KilledHypothesisSummary
    from tradegy.auto_generation.market_scan import MarketScanReport


@dataclass(frozen=True)
class GenerationContext:
    """Shared context passed to both generators.

    The generator may use these to constrain its output (variant
    generators MUST stay within `available_class_ids` and
    `available_feature_ids`; hypothesis generators SHOULD prefer
    feature_dependencies the registry already supports).
    """

    available_class_ids: tuple[str, ...]
    available_feature_ids: tuple[str, ...]
    available_condition_ids: tuple[str, ...] = ()
    """Registered condition evaluators — used in gating_conditions
    and invalidation_conditions on generated specs."""

    available_sizing_methods: tuple[str, ...] = ()
    available_stop_methods: tuple[str, ...] = ()

    feature_stats: dict[str, "FeatureStats"] = field(default_factory=dict)
    """Per-feature distribution snapshot (rows, min, max, p10, median,
    p90). Populated by the CLI from `feature_stats.compute_all_*`;
    rendered alongside each feature id in the cached registry block
    so the LLM proposes thresholds inside the live distribution.
    Empty dict → fall back to no-stats rendering (pre-2026-05-02
    behaviour)."""

    kill_summaries: tuple["KilledHypothesisSummary", ...] = ()
    """Recently-killed hypotheses (status=killed/retired or every
    variant discarded). Rendered as a "do not propose mechanistic
    near-duplicates" block in the hypothesis-generator prompt so the
    LLM knows what's already been tried and failed. Empty tuple →
    no kill block emitted (clean slate or test stub)."""

    market_scan_report: "MarketScanReport | None" = None
    """Snapshot of current-vs-baseline market-structure observations
    (vol regime, gap behaviour, session-position concentration of
    large moves, volume profile). Rendered as a "current market-
    structure observations" block in the hypothesis-generator prompt
    so the LLM is anchored in regimes that exist *now* rather than
    canonical training-corpus patterns. None → no observation block
    emitted."""

    instrument_scope: tuple[str, ...] = ("MES",)
    extra: dict[str, Any] = None  # type: ignore[assignment]


class HypothesisGenerator(ABC):
    """Produces structured Hypothesis records.

    `generate(seed_context, n)` returns AT MOST n hypotheses. The
    caller validates each against the schema and decides which to
    promote (manual triage per `06_hypothesis_system.md` Stage 3,
    or scored auto-triage in a future revision).
    """

    id: str = "<override>"

    @abstractmethod
    def generate(
        self,
        *,
        seed: str,
        context: GenerationContext,
        n: int,
    ) -> list[Hypothesis]:
        ...


class VariantGenerator(ABC):
    """Given one Hypothesis, produces N strategy-spec variants.

    Doc 07 §50-63 enumerates the constraints: registered classes /
    features only; no parameter-envelope violations; meaningfully
    distinct variants. The base class doesn't enforce these (the
    AutoTestOrchestrator's pre-backtest validator does); concrete
    impls SHOULD however bias their LLM prompts toward compliance.
    """

    id: str = "<override>"

    @abstractmethod
    def generate(
        self,
        *,
        hypothesis: Hypothesis,
        context: GenerationContext,
        n: int,
    ) -> list[StrategySpec]:
        ...


# ── Stubs (test-only) ───────────────────────────────────────────


class StubHypothesisGenerator(HypothesisGenerator):
    """Returns a fixed list of Hypothesis objects supplied at
    construction time. Tests use this to feed the orchestrator
    without an LLM.
    """

    id = "stub_hypothesis_generator"

    def __init__(self, canned: list[Hypothesis]) -> None:
        self._canned = list(canned)

    def generate(
        self, *, seed: str, context: GenerationContext, n: int,
    ) -> list[Hypothesis]:
        return list(self._canned[:n])


class StubVariantGenerator(VariantGenerator):
    """Returns a fixed list of StrategySpec objects. Tests pre-build
    spec drafts (or load existing YAMLs) and pass them in.
    """

    id = "stub_variant_generator"

    def __init__(self, canned: list[StrategySpec]) -> None:
        self._canned = list(canned)

    def generate(
        self,
        *,
        hypothesis: Hypothesis,
        context: GenerationContext,
        n: int,
    ) -> list[StrategySpec]:
        return list(self._canned[:n])
