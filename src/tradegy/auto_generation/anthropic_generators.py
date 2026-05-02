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

import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tradegy.auto_generation.cost import (
    CostEstimate,
    cost_for_usage,
    format_cost_line,
)
from tradegy.auto_generation.feature_stats import format_feature_stats
from tradegy.auto_generation.generators import (
    GenerationContext,
    HypothesisGenerator,
    VariantGenerator,
)
from tradegy.auto_generation.kill_log import format_kill_summaries
from tradegy.auto_generation.market_scan import format_market_scan_report
from tradegy.auto_generation.hypothesis import (
    FiveTestScores,
    Hypothesis,
    ParameterRangeSpec,
)
from tradegy.specs.schema import (
    EntrySpec,
    ExitsSpec,
    GatingCondition,
    InvalidationCondition,
    MarketScopeSpec,
    MetadataSpec,
    OperationalSpec,
    SizingSpec,
    StopsSpec,
    StrategySpec,
    TimeStopBlock,
)


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


_JSON_FENCE_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```",
    re.DOTALL,
)


def _extract_json_blob(text: str) -> str:
    """Pull a JSON object/array out of an LLM text response.

    Tolerates the common formats: bare JSON, ```json fences, ``` fences,
    or JSON wrapped in any explanatory prose. Returns the longest blob
    that parses as JSON.
    """
    # Prefer fenced code blocks first.
    fence_matches = _JSON_FENCE_RE.findall(text)
    candidates = list(fence_matches)
    # Add the largest balanced object/array we can find as a fallback.
    obj_start = text.find("{")
    obj_end = text.rfind("}")
    if obj_start != -1 and obj_end > obj_start:
        candidates.append(text[obj_start : obj_end + 1])
    arr_start = text.find("[")
    arr_end = text.rfind("]")
    if arr_start != -1 and arr_end > arr_start:
        candidates.append(text[arr_start : arr_end + 1])

    for blob in candidates:
        try:
            json.loads(blob)
            return blob
        except json.JSONDecodeError:
            continue
    raise ValueError(
        "could not extract a parseable JSON blob from the LLM response"
    )


def _parse_with_schema(text: str, schema: type[BaseModel]) -> BaseModel:
    """Extract JSON from prose, then Pydantic-validate against `schema`."""
    blob = _extract_json_blob(text)
    try:
        return schema.model_validate_json(blob)
    except ValidationError as exc:
        raise ValueError(
            f"LLM output did not match {schema.__name__}: {exc}"
        ) from exc


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


def _normalize_session(s: str) -> str:
    """LLMs often emit case variants ('rth', 'RTH', 'Globex'). The
    StrategySpec literal is case-sensitive, so we map robustly.
    """
    canonical = {"rth": "RTH", "globex": "globex", "both": "both"}
    return canonical.get(s.strip().lower(), s)


def _normalize_direction(s: str) -> str:
    """`EntrySpec.direction` literal is lowercase."""
    return s.strip().lower()


def _coerce_condition_entry(raw: Any) -> dict:
    """Normalize one gating/invalidation condition record.

    LLMs sometimes emit the registry name under `condition` (correct),
    `name`, `type`, or `evaluator`. We accept any of these and surface
    a clear error otherwise. Parameters are likewise pulled from
    `parameters`, `params`, or `args` if present.
    """
    if not isinstance(raw, dict):
        raise ValueError(
            f"condition entry must be a JSON object; got {type(raw).__name__}"
        )
    name = (
        raw.get("condition")
        or raw.get("name")
        or raw.get("type")
        or raw.get("evaluator")
    )
    if not name:
        raise ValueError(
            "condition entry missing 'condition' (or 'name' / 'type' / "
            f"'evaluator'); keys present: {sorted(raw.keys())}"
        )
    params = raw.get("parameters") or raw.get("params") or raw.get("args") or {}
    if not isinstance(params, dict):
        raise ValueError(
            f"condition parameters must be an object; got "
            f"{type(params).__name__}"
        )
    action = raw.get("action", "exit_market")
    return {"condition": name, "parameters": dict(params), "action": action}


def _safe_loads(blob: str, default: Any) -> Any:
    """`json.loads` with a fallback. Empty / whitespace strings → default."""
    import json

    if not blob or not blob.strip():
        return default
    try:
        return json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"LLM emitted invalid JSON in a draft field: {exc}"
        ) from exc


def _draft_to_spec(
    draft: StrategySpecDraft, *, author: str, today: date,
) -> StrategySpec:
    """Wrap a compact LLM draft into a full StrategySpec.

    The draft is the creative content; the boilerplate (schema_version,
    status, dates, author, defaults for risk_envelope etc.) is added
    here so the spec passes the harness's `validate_spec` invariants.
    JSON-string fields on the draft are parsed and re-validated by the
    full schema.
    """
    entry_params = _safe_loads(draft.entry_parameters_json, {})
    gating_raw = _safe_loads(draft.gating_conditions_json, [])
    sizing_params = _safe_loads(draft.sizing_parameters_json, {"contracts": 1})
    initial_stop_params = _safe_loads(draft.initial_stop_parameters_json, {})
    invalidation_raw = _safe_loads(draft.invalidation_conditions_json, [])

    return StrategySpec(
        metadata=MetadataSpec(
            id=draft.spec_id,
            version="0.1.0",
            schema_version="1.0",
            name=draft.name,
            status="draft",
            created_date=today,
            last_modified_date=today,
            author=author,
            description=draft.description,
        ),
        market_scope=MarketScopeSpec(
            instrument=draft.instrument,
            session=_normalize_session(draft.session),  # type: ignore[arg-type]
        ),
        entry=EntrySpec(
            strategy_class=draft.strategy_class,
            parameters=dict(entry_params),
            direction=_normalize_direction(draft.direction),  # type: ignore[arg-type]
            entry_order_type=draft.entry_order_type.strip().lower(),  # type: ignore[arg-type]
            gating_conditions=[
                GatingCondition(
                    condition=norm["condition"],
                    parameters=norm["parameters"],
                )
                for norm in (_coerce_condition_entry(g) for g in gating_raw)
            ],
        ),
        sizing=SizingSpec(
            method=draft.sizing_method,
            parameters=dict(sizing_params),
        ),
        stops=StopsSpec(
            initial_stop={
                "method": draft.initial_stop_method,
                **dict(initial_stop_params),
            },
            adjustment_rules=[],
            hard_max_distance_ticks=draft.hard_max_distance_ticks,
            time_stop=TimeStopBlock(
                enabled=True,
                max_holding_bars=draft.time_stop_max_holding_bars,
                action_at_time_stop="exit_market",
            ),
        ),
        exits=ExitsSpec(
            invalidation_conditions=[
                InvalidationCondition(
                    condition=norm["condition"],
                    parameters=norm["parameters"],
                    action=norm["action"],  # type: ignore[arg-type]
                )
                for norm in (_coerce_condition_entry(c) for c in invalidation_raw)
            ],
        ),
        operational=OperationalSpec(tier="proposal_only"),
    )


def _registry_context_block(ctx: GenerationContext) -> str:
    """The cacheable system-prompt section listing the registry. Lists
    every primitive a generated spec can reference, plus the parameter
    shapes for the most common evaluators / stops so the LLM doesn't
    invent inline DSL.

    Feature ids include their (rows, min/max, p10/median/p90)
    distribution stats when `ctx.feature_stats` is populated. This
    grounds threshold proposals in the live data — the dry run on
    2026-05-01 produced unfireable variants because the LLM picked
    threshold values without knowing the feature's distribution.
    """

    def _list(items: tuple[str, ...]) -> str:
        return "\n".join(f"  - {x}" for x in items) if items else "  (none)"

    if ctx.feature_stats:
        feature_lines = "\n".join(
            format_feature_stats(ctx.feature_stats[fid])
            if fid in ctx.feature_stats
            else f"  - {fid}: (no stats)"
            for fid in ctx.available_feature_ids
        )
        feature_section_intro = (
            "\n\nAvailable feature ids (with live distribution; "
            "anchor thresholds inside [p10, p90]):\n"
        )
    else:
        feature_lines = _list(ctx.available_feature_ids)
        feature_section_intro = "\n\nAvailable feature ids:\n"

    return (
        "## Registry context (use ONLY these)\n\n"
        "Available strategy classes:\n"
        + _list(ctx.available_class_ids)
        + feature_section_intro
        + feature_lines
        + "\n\nAvailable condition evaluators (used in gating_conditions"
        " + invalidation_conditions). Each entry is a JSON object with"
        " `condition` (name from this list) and `parameters` (per-"
        "evaluator):\n"
        + _list(ctx.available_condition_ids)
        + "\n  Common evaluator parameter shapes:\n"
        '  * feature_threshold:  {"feature_id": "...", '
        '"operator": "gt|gte|lt|lte|eq", "threshold": NUMBER}\n'
        '  * feature_range:      {"feature_id": "...", '
        '"lo": NUMBER, "hi": NUMBER}\n'
        '  * time_of_session:    {"session_position_feature_id": '
        '"mes_session_position" or "mes_xnys_session_position", '
        '"lo": 0..1, "hi": 0..1}\n'
        "  Threshold values must lie inside the feature's reported "
        "distribution. Proposing thresholds outside [min, max] or in "
        "the deep tails (< p10 / > p90) yields unfireable variants — "
        "wasted budget.\n"
        + "\n\nAvailable sizing methods:\n"
        + _list(ctx.available_sizing_methods)
        + "\n\nAvailable initial-stop methods:\n"
        + _list(ctx.available_stop_methods)
        + "\n  Common stop parameter shapes:\n"
        '  * fixed_ticks:   {"stop_ticks": INT, "tick_size": 0.25}\n'
        '  * atr_multiple:  {"atr_feature_id": "mes_atr_14m", '
        '"multiplier": NUMBER, "max_distance_ticks": INT, '
        '"tick_size": 0.25}\n'
        "\n  ATR-stop hard rule: when using atr_multiple, the spec MUST\n"
        "  also include a feature_threshold gating_condition on the SAME\n"
        "  ATR feature with operator=lt and threshold strictly less than\n"
        "  (max_distance_ticks * tick_size / multiplier). Otherwise an\n"
        "  extreme-vol bar (e.g. COVID 2020-03-16) will produce an\n"
        "  ATR-derived stop offset > the cap, the harness will RAISE\n"
        "  mid-backtest, and the variant scores as a runtime error\n"
        "  instead of a real test. Pick max_distance_ticks generously\n"
        "  AND gate the ATR feature explicitly — the cap is the\n"
        "  fail-safe, the gate is the design.\n"
        f"\nInstrument scope: {', '.join(ctx.instrument_scope)}\n"
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

        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": self._effort},
            system=system_blocks,
            messages=[{"role": "user", "content": user_text}],
        )
        self._record_cost(response)

        text = "".join(
            b.text for b in response.content if getattr(b, "type", None) == "text"
        )
        batch = _parse_with_schema(text, HypothesisDraftBatch)
        out: list[Hypothesis] = []
        for draft in batch.hypotheses[:n]:
            out.append(_draft_to_hypothesis(draft, author=self._author))
        return out

    # ── Prompt assembly ─────────────────────────────────────────

    def _build_system(self, ctx: GenerationContext) -> list[dict]:
        """System prompt assembly. Layout:

          1. stable frame (hard constraints)
          2. registry context (admitted classes/features/evaluators) —
             carries `cache_control` so subsequent calls within the
             5-minute TTL read at ~10% of input cost
          3. kill log (recent failed hypotheses) — dynamic between
             calls, placed AFTER the cache breakpoint so its churn
             doesn't invalidate the cached prefix
          4. market-scan observations — same dynamic-after-cache
             treatment

        Blocks 3 and 4 are emitted only when their context is
        populated — this preserves backward compatibility with tests
        that build a bare GenerationContext.
        """
        blocks: list[dict] = [
            {"type": "text", "text": _HYPOTHESIS_SYSTEM_FRAME},
            {
                "type": "text",
                "text": _registry_context_block(ctx),
                "cache_control": {"type": "ephemeral"},
            },
        ]
        if ctx.kill_summaries:
            kill_text = format_kill_summaries(ctx.kill_summaries)
            if kill_text:
                blocks.append({"type": "text", "text": kill_text})
        if ctx.market_scan_report is not None:
            scan_text = format_market_scan_report(ctx.market_scan_report)
            if scan_text:
                blocks.append({"type": "text", "text": scan_text})
        return blocks

    def _build_user(self, *, seed: str, n: int) -> str:
        seed_section = (
            f"Seed direction (optional): {seed}\n\n"
            if seed.strip()
            else "Seed direction: (none — generate freely from market-structure or "
            "practitioner-canon sources).\n\n"
        )
        schema_json = json.dumps(
            HypothesisDraftBatch.model_json_schema(), indent=2,
        )
        return (
            f"Generate {n} new hypothesis drafts.\n\n"
            f"{seed_section}"
            "Each hypothesis must include a concrete mechanism and a quantitative "
            "falsification criterion. Reference only the registered classes and "
            "features listed above. Vary mechanisms across the batch — duplicates "
            "are wasted budget.\n\n"
            "Respond with a single JSON object matching this schema, wrapped in "
            "a ```json code fence and nothing else (no preamble, no trailing "
            "prose):\n\n"
            f"```json-schema\n{schema_json}\n```"
        )

    def _record_cost(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        self.last_response_usage = usage
        if usage is not None:
            self.last_cost = cost_for_usage(self._model, usage)
            _log.info("hypothesis-gen cost: %s", format_cost_line(self.last_cost))


# ─── Variant generator ────────────────────────────────────────────


class StrategySpecDraft(_Strict):
    """Compact, LLM-friendly subset of a StrategySpec.

    Server-side strict-output grammar compilation rejects schemas with
    free-form `dict[str, Any]` fields ("Schema is too complex"). To
    work around that, every dynamic dict in this draft is a JSON
    STRING — the LLM emits valid JSON text, we `json.loads` it on the
    Python side, and Pydantic validation happens against the resulting
    full StrategySpec.
    """

    spec_id: str = Field(min_length=4)
    name: str
    description: str = ""
    instrument: str = "MES"
    session: str = "globex"  # "RTH" | "globex" | "both"

    strategy_class: str
    entry_parameters_json: str = "{}"
    """JSON object string. Keys/values match the strategy class's
    parameter_schema (see `03_strategy_class_registry.md` for each
    class's contract). Example: '{"vwap_feature_id": "mes_vwap",
    "deviation_threshold_ticks": 8}'."""

    direction: str = "long"  # "long" | "short" | "both"
    entry_order_type: str = "market"

    gating_conditions_json: str = "[]"
    """JSON array string of {condition, parameters} objects. Each
    condition must resolve in the condition-evaluator registry.
    Example: '[{"condition": "feature_range", "parameters":
    {"feature_id": "mes_realized_vol_30m", "lo": 0.08, "hi": 0.22}}]'."""

    sizing_method: str = "fixed_contracts"
    sizing_parameters_json: str = '{"contracts": 1}'

    initial_stop_method: str = "fixed_ticks"
    """e.g. 'fixed_ticks' or 'atr_multiple'."""

    initial_stop_parameters_json: str = "{}"
    """JSON object string. For fixed_ticks: {"stop_ticks": 12,
    "tick_size": 0.25}. For atr_multiple: {"atr_feature_id":
    "mes_atr_14m", "multiplier": 2.0, "max_distance_ticks": 200}."""

    hard_max_distance_ticks: int = 200
    time_stop_max_holding_bars: int = 60

    invalidation_conditions_json: str = "[]"
    """JSON array string of {condition, parameters, action} objects."""

    rationale: str = ""
    """One-line explanation of how this variant differs from siblings."""


class StrategySpecBatch(_Strict):
    """LLM output for `messages.parse()` — wraps a list of drafts in a
    single object (top-level lists aren't allowed).
    """

    specs: list[StrategySpecDraft]


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
        max_tokens: int = 16_000,
    ) -> None:
        # 16K keeps the non-streaming `messages.parse()` call under the
        # SDK's 10-minute timeout guard. Spec batches of 5-8 variants
        # comfortably fit; if a future hypothesis needs more output,
        # bump max_tokens AND switch to streaming (the SDK refuses
        # non-streaming above ~16K).
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

        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": self._effort},
            system=system_blocks,
            messages=[{"role": "user", "content": user_text}],
        )
        self._record_cost(response)

        text = "".join(
            b.text for b in response.content if getattr(b, "type", None) == "text"
        )
        batch = _parse_with_schema(text, StrategySpecBatch)
        # Convert each compact draft into a full StrategySpec.
        today = datetime.now(tz=timezone.utc).date()
        out: list[StrategySpec] = []
        for draft in batch.specs[:n]:
            out.append(_draft_to_spec(draft, author=self._author, today=today))
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
        schema_json = json.dumps(
            StrategySpecBatch.model_json_schema(), indent=2,
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

Generate {n} variant strategy spec drafts. Each spec_id should follow
"{hypothesis.id}__variant_<letter>" (e.g. "_variant_a", "_variant_b").
Make the variants mechanistically distinct, not just numerically
different. The dynamic-parameter fields are JSON STRINGS (not nested
objects) — emit valid JSON text inside each `*_json` field.

Respond with a single JSON object matching this schema, wrapped in a
```json code fence and nothing else (no preamble, no trailing prose):

```json-schema
{schema_json}
```"""

    def _record_cost(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        self.last_response_usage = usage
        if usage is not None:
            self.last_cost = cost_for_usage(self._model, usage)
            _log.info("variant-gen cost: %s", format_cost_line(self.last_cost))
