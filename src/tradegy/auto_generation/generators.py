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
from dataclasses import dataclass
from typing import Any

from tradegy.auto_generation.hypothesis import Hypothesis
from tradegy.specs.schema import StrategySpec


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
