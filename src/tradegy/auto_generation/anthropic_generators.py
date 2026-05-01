"""Anthropic-SDK-backed hypothesis + variant generators.

Phase B of doc 07's auto-generation pipeline. Production code that
calls Claude to draft hypotheses and strategy-spec variants. Tests
inject a fake client; production uses the real `anthropic.Anthropic`.

Design notes:

- **Model + thinking**: opus-4-7 with adaptive thinking. Effort is
  tunable per call; default `high` for hypothesis ideation, `medium`
  for variant authoring. Adaptive thinking is OFF by default on Opus
  4.7 — we enable it explicitly for both flows.
- **Prompt caching**: the registry context (admitted classes,
  features, instrument scope) is identical across calls for a given
  registry snapshot, so we put it at the top of the system prompt
  and apply `cache_control` on the last system block. This caches
  tools (none here) + system together; subsequent calls within the
  5-minute TTL read at ~10% of input cost.
- **Structured output**: we use `client.messages.parse()` with
  Pydantic models so each LLM response is schema-validated before
  it touches the rest of the pipeline. Failed validation surfaces
  as an exception the orchestrator can catch and skip.
- **Draft → full**: the LLM returns "drafts" — the creative parts of
  a Hypothesis or StrategySpec. The generator wraps each draft with
  the bookkeeping fields (id, status, created_date, author) before
  returning a fully-validated record.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from tradegy.auto_generation.cost import (
    CostEstimate,
    cost_for_usage,
    format_cost_line,
)
from tradegy.auto_generation.generators import (
    GenerationContext,
    HypothesisGenerator,
    VariantGenerator,
)
from tradegy.auto_generation.hypothesis import (
    FiveTestScores,
    Hypothesis,
    ParameterRangeSpec,
)
from tradegy.specs.schema import StrategySpec


_log = logging.getLogger(__name__)


DEFAULT_MODEL = "claude-opus-4-7"


# ─── LLM output schemas ────────────────────────────────────────────


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HypothesisDraft(_Strict):
    """What the LLM produces for one hypothesis. The generator wraps
    it with id/status/dates/author before returning a full Hypothesis.
    Field set is the doc 06 minimum: mechanism + falsification are
    load-bearing per design principles 2 + 3.
    """

    title: str = Field(min_length=4, max_length=120)
    observation: str
    mechanism: str
    falsification: str
    counterparty: str = ""
    instrument_scope: list[str] = Field(min_length=1)
    feature_dependencies: list[str] = Field(default_factory=list)
    parameter_envelope: list[ParameterRangeSpec] = Field(default_factory=list)
    suggested_variant_budget: int = Field(ge=3, le=15)
    five_test_scores: FiveTestScores = Field(default_factory=FiveTestScores)
    notes: str = ""


class HypothesisDraftBatch(_Strict):
    """Wrapper batch — Pydantic schemas for `messages.parse()` must be
    a single object, not a top-level list.
    """

    hypotheses: list[HypothesisDraft]


# ─── Helpers ───────────────────────────────────────────────────────


def _slugify(s: str, *, max_len: int = 48) -> str:
    """Conservative slug for hypothesis / spec ids."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return cleaned[:max_len].strip("_") or "hypothesis"


def _draft_to_hypothesis(
    draft: HypothesisDraft,
    *,
    author: str,
    today: date | None = None,
    status: str = "proposed",
) -> Hypothesis:
    """Wrap an LLM draft with the bookkeeping fields and validate."""
    today = today or datetime.now(tz=timezone.utc).date()
    suffix = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:21]
    hid = f"hyp_{_slugify(draft.title)}_{suffix}"
    return Hypothesis(
        id=hid,
        title=draft.title,
        status=status,  # type: ignore[arg-type]
        source="llm_brainstorm",
        created_date=today,
        last_modified_date=today,
        author=author,
        observation=draft.observation,
        mechanism=draft.mechanism,
        falsification=draft.falsification,
        counterparty=draft.counterparty,
        instrument_scope=draft.instrument_scope,
        feature_dependencies=draft.feature_dependencies,
        parameter_envelope=draft.parameter_envelope,
        variant_budget=draft.suggested_variant_budget,
        five_test_scores=draft.five_test_scores,
        notes=draft.notes,
    )


def _registry_context_block(ctx: GenerationContext) -> str:
    """The cacheable system-prompt section listing the registry."""
    return (
        "## Registry context (use ONLY these)\n\n"
        f"Available strategy classes:\n"
        + "\n".join(f"  - {c}" for c in ctx.available_class_ids)
        + "\n\nAvailable feature ids:\n"
        + "\n".join(f"  - {f}" for f in ctx.available_feature_ids)
        + f"\n\nInstrument scope: {', '.join(ctx.instrument_scope)}\n"
    )


# ─── Hypothesis generator ─────────────────────────────────────────


_HYPOTHESIS_SYSTEM_FRAME = """You are a quantitative-strategy hypothesis generator for the tradegy
project (futures trading on CME E-mini index futures, currently MES).

Your job: produce concrete, falsifiable trading hypotheses that downstream
auto-generation can turn into testable strategy-spec variants.

Hard constraints (from `06_hypothesis_system.md`):

1. Mechanism is required. Each hypothesis must state who trades, why,
   under what constraints, and what pattern therefore exists. A
   pattern observation without a proposed mechanism is not a
   hypothesis.
2. Falsification is required. Each hypothesis must state what observed
   result would refute it.
3. Use only registered classes and features (listed below). If your
   mechanism implies a feature that doesn't exist yet, NAME IT in
   `feature_dependencies` and the operator will decide whether to admit
   it — do not invent a substitute.
4. Variant budget should be 3-10 per hypothesis (hard cap 15).
5. Avoid mechanistic near-duplicates of failed prior hypotheses.

Output style:

- Be concrete. "Mean reversion in equities" is not a hypothesis;
  "Front-month VX backwardation steeper than 5-day median predicts
  same-day MES afternoon mean reversion because dealers rebalance
  hedges into the close" IS one.
- Falsification must name a specific quantitative gate that would
  kill the hypothesis (not "if it doesn't work").
- Suggest a parameter envelope only for mechanisms that are tunable;
  otherwise leave the list empty.
"""


class AnthropicHypothesisGenerator(HypothesisGenerator):
    """LLM-driven hypothesis ideation.

    Uses the Anthropic SDK's `messages.parse()` to get a
    `HypothesisDraftBatch` back, then wraps each draft into a
    full Hypothesis record. Cost is logged after the call via the
    `last_cost` attribute and `last_response_usage` for caller
    inspection.
    """

    id = "anthropic_hypothesis_generator_v1"

    def __init__(
        self,
        *,
        client: Any,  # anthropic.Anthropic — duck-typed for testability
        model: str = DEFAULT_MODEL,
        author_label: str | None = None,
        effort: str = "high",
        max_tokens: int = 16_000,
    ) -> None:
        self._client = client
        self._model = model
        self._author = author_label or model
        self._effort = effort
        self._max_tokens = max_tokens
        self.last_cost: CostEstimate | None = None
        self.last_response_usage: Any = None

    def generate(
        self,
        *,
        seed: str,
        context: GenerationContext,
        n: int,
    ) -> list[Hypothesis]:
        if n <= 0:
            return []
        system_blocks = self._build_system(context)
        user_text = self._build_user(seed=seed, n=n)

        response = self._client.messages.parse(
            model=self._model,
            max_tokens=self._max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": self._effort},
            system=system_blocks,
            messages=[{"role": "user", "content": user_text}],
            output_format=HypothesisDraftBatch,
        )
        self._record_cost(response)

        batch: HypothesisDraftBatch = response.parsed_output
        out: list[Hypothesis] = []
        for draft in batch.hypotheses[:n]:
            out.append(_draft_to_hypothesis(draft, author=self._author))
        return out

    # ── Prompt assembly ─────────────────────────────────────────

    def _build_system(self, ctx: GenerationContext) -> list[dict]:
        """Two-block system prompt: stable frame first, then the
        cacheable registry context with `cache_control` on the last
        block per `shared/prompt-caching.md`.
        """
        return [
            {"type": "text", "text": _HYPOTHESIS_SYSTEM_FRAME},
            {
                "type": "text",
                "text": _registry_context_block(ctx),
                "cache_control": {"type": "ephemeral"},
            },
        ]

    def _build_user(self, *, seed: str, n: int) -> str:
        seed_section = (
            f"Seed direction (optional): {seed}\n\n"
            if seed.strip()
            else "Seed direction: (none — generate freely from market-structure or "
            "practitioner-canon sources).\n\n"
        )
        return (
            f"Generate {n} new hypothesis drafts as a JSON object matching the "
            "`HypothesisDraftBatch` schema.\n\n"
            f"{seed_section}"
            "Each hypothesis must include a concrete mechanism and a quantitative "
            "falsification criterion. Reference only the registered classes and "
            "features listed above. Vary mechanisms across the batch — duplicates "
            "are wasted budget."
        )

    def _record_cost(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        self.last_response_usage = usage
        if usage is not None:
            self.last_cost = cost_for_usage(self._model, usage)
            _log.info("hypothesis-gen cost: %s", format_cost_line(self.last_cost))


# ─── Variant generator ────────────────────────────────────────────


class StrategySpecBatch(_Strict):
    """LLM output for `messages.parse()`. The list-of-StrategySpec
    case requires wrapping in a single object. Each spec is the full
    schema — Pydantic enforces every constraint at parse time.
    """

    specs: list[StrategySpec]


_VARIANT_SYSTEM_FRAME = """You are a strategy-spec variant generator for the tradegy project.

Given one promoted hypothesis, your job is to produce N concrete strategy-
spec drafts that mechanically express the hypothesis using only registered
classes, features, and parameter ranges.

Hard constraints (from `07_auto_generation.md` § 50-63):

1. Use only the strategy classes, features, and feature ids listed in the
   registry context.
2. Stay within the hypothesis's declared `parameter_envelope`.
3. Each variant must differ meaningfully from the others in at least one
   of: trigger formulation, confirmation filter, exit logic (target /
   stop / invalidation), timeframe / window parameters, or feature
   dependencies.
4. Do not produce near-duplicates. Two specs that differ only in a
   numeric threshold (e.g. 0.001 vs 0.002) are duplicates unless the
   parameter envelope explicitly justifies the gradient.
5. Every spec must conform to `04_strategy_spec_schema.md`. The
   harness will reject specs whose class / feature references are
   not registered.

Each spec's `metadata`:
- `id` should be a short slug ending in `_a`, `_b`, `_c`, etc., to
  distinguish siblings within the batch
- `status` = "draft", `tier` (under operational) = "proposal_only"
- `created_date` / `last_modified_date` will be filled by the caller
- `author` will be filled by the caller

Pre-registration: the variant set is locked once you emit it. Doc 07's
post-hoc rules forbid expanding the batch after seeing results. Make
the variants count.
"""


class AnthropicVariantGenerator(VariantGenerator):
    """LLM-driven strategy-spec variant generator.

    Returns a list of `StrategySpec` instances validated by Pydantic.
    Every spec is checked against the existing `validate_spec` registry-
    resolution invariants downstream by the orchestrator before any
    backtest runs — this generator's job is to produce specs that make
    it through that gate, not to invent novel mechanics.
    """

    id = "anthropic_variant_generator_v1"

    def __init__(
        self,
        *,
        client: Any,
        model: str = DEFAULT_MODEL,
        author_label: str | None = None,
        effort: str = "medium",
        max_tokens: int = 32_000,
    ) -> None:
        self._client = client
        self._model = model
        self._author = author_label or model
        self._effort = effort
        self._max_tokens = max_tokens
        self.last_cost: CostEstimate | None = None
        self.last_response_usage: Any = None

    def generate(
        self,
        *,
        hypothesis: Hypothesis,
        context: GenerationContext,
        n: int,
    ) -> list[StrategySpec]:
        if n <= 0:
            return []
        system_blocks = self._build_system(context)
        user_text = self._build_user(hypothesis=hypothesis, n=n)

        response = self._client.messages.parse(
            model=self._model,
            max_tokens=self._max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": self._effort},
            system=system_blocks,
            messages=[{"role": "user", "content": user_text}],
            output_format=StrategySpecBatch,
        )
        self._record_cost(response)

        batch: StrategySpecBatch = response.parsed_output
        # Stamp author + created_date on every spec the LLM returns
        # so downstream `validate_spec` is happy. The LLM provides the
        # creative content; we own the bookkeeping.
        today = datetime.now(tz=timezone.utc).date()
        out: list[StrategySpec] = []
        for spec in batch.specs[:n]:
            spec.metadata.author = self._author
            spec.metadata.created_date = today
            spec.metadata.last_modified_date = today
            out.append(spec)
        return out

    # ── Prompt assembly ─────────────────────────────────────────

    def _build_system(self, ctx: GenerationContext) -> list[dict]:
        return [
            {"type": "text", "text": _VARIANT_SYSTEM_FRAME},
            {
                "type": "text",
                "text": _registry_context_block(ctx),
                "cache_control": {"type": "ephemeral"},
            },
        ]

    def _build_user(self, *, hypothesis: Hypothesis, n: int) -> str:
        env_text = (
            "\n".join(
                f"  - {p.name}: [{p.min}, {p.max}]"
                + (f" step {p.step}" if p.step is not None else "")
                for p in hypothesis.parameter_envelope
            )
            if hypothesis.parameter_envelope
            else "  (no envelope declared — choose conservative defaults)"
        )
        feature_deps = (
            ", ".join(hypothesis.feature_dependencies)
            if hypothesis.feature_dependencies
            else "(unconstrained — use any registered feature)"
        )
        return f"""Hypothesis under test:

Title: {hypothesis.title}
Observation: {hypothesis.observation}
Mechanism: {hypothesis.mechanism}
Falsification: {hypothesis.falsification}
Counterparty: {hypothesis.counterparty or "(unspecified)"}
Instrument scope: {", ".join(hypothesis.instrument_scope)}

Feature dependencies declared on the hypothesis: {feature_deps}

Parameter envelope (variants must stay within these bounds):
{env_text}

Generate {n} variant strategy specs as a JSON object matching the
`StrategySpecBatch` schema. Each spec id should follow the
"{hypothesis.id}__variant_<letter>" pattern. Make the variants
mechanistically distinct, not just numerically different."""

    def _record_cost(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        self.last_response_usage = usage
        if usage is not None:
            self.last_cost = cost_for_usage(self._model, usage)
            _log.info("variant-gen cost: %s", format_cost_line(self.last_cost))
