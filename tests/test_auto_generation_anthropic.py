"""AnthropicHypothesisGenerator + AnthropicVariantGenerator tests.

Anthropic SDK calls are mocked — no real API access. Tests verify:
  - the prompt is built with cacheable registry context
  - the LLM-returned Pydantic batch round-trips through draft → full
  - bookkeeping fields (id, dates, author) get filled by the generator
  - cost is recorded after the call
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pytest

from tradegy.auto_generation.anthropic_generators import (
    AnthropicHypothesisGenerator,
    AnthropicVariantGenerator,
    HypothesisDraft,
    HypothesisDraftBatch,
    StrategySpecBatch,
)
from tradegy.auto_generation.generators import GenerationContext
from tradegy.auto_generation.hypothesis import (
    Hypothesis,
    ParameterRangeSpec,
)
from tradegy.specs.schema import (
    EntrySpec, ExitsSpec, MarketScopeSpec, MetadataSpec, SizingSpec,
    StopsSpec, StrategySpec, TimeStopBlock,
)


# ── Fake Anthropic client ──────────────────────────────────────


@dataclass
class _FakeUsage:
    input_tokens: int = 1000
    output_tokens: int = 500
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _FakeResponse:
    parsed_output: Any
    usage: _FakeUsage = field(default_factory=_FakeUsage)


class _FakeMessages:
    """Mocks `client.messages.parse()`. Returns a pre-set parsed_output
    and records the kwargs the caller passed in for assertions.
    """

    def __init__(self, parsed_output: Any) -> None:
        self.parsed_output = parsed_output
        self.last_call_kwargs: dict | None = None

    def parse(self, **kwargs):
        self.last_call_kwargs = kwargs
        return _FakeResponse(parsed_output=self.parsed_output)


class _FakeClient:
    def __init__(self, parsed_output: Any) -> None:
        self.messages = _FakeMessages(parsed_output)


# ── Fixtures ────────────────────────────────────────────────────


def _ctx() -> GenerationContext:
    return GenerationContext(
        available_class_ids=("vwap_reversion", "gap_fill_fade"),
        available_feature_ids=("mes_vwap", "mes_prior_rth_close"),
        instrument_scope=("MES",),
    )


def _hypothesis_draft(title: str = "Test hypothesis") -> HypothesisDraft:
    return HypothesisDraft(
        title=title,
        observation="MES gaps fill toward prior RTH close intraday.",
        mechanism=(
            "Overnight order-flow imbalance creates gaps that get unwound "
            "in the early RTH session by participants buying/selling against."
        ),
        falsification=(
            "If avg OOS Sharpe over walk-forward < 0.0 across 3+ folds, "
            "the mechanism is invalidated."
        ),
        counterparty="overnight risk-off flow",
        instrument_scope=["MES"],
        feature_dependencies=["mes_prior_rth_close"],
        parameter_envelope=[
            ParameterRangeSpec(name="gap_threshold_pct", min=0.001, max=0.02),
        ],
        suggested_variant_budget=5,
    )


def _strategy_spec(spec_id: str = "auto_v_a") -> StrategySpec:
    return StrategySpec(
        metadata=MetadataSpec(
            id=spec_id, version="0.1.0",
            name="LLM-drafted variant",
            created_date=date(2026, 5, 1),
            last_modified_date=date(2026, 5, 1),
            author="LLM",
        ),
        market_scope=MarketScopeSpec(instrument="MES"),
        entry=EntrySpec(
            strategy_class="vwap_reversion",
            parameters={"vwap_feature_id": "mes_vwap"},
        ),
        sizing=SizingSpec(method="fixed_contracts", parameters={"contracts": 1}),
        stops=StopsSpec(
            initial_stop={"method": "fixed_ticks", "stop_ticks": 12, "tick_size": 0.25},
            time_stop=TimeStopBlock(enabled=True, max_holding_bars=30),
        ),
        exits=ExitsSpec(),
    )


# ── Hypothesis generator ──────────────────────────────────────


def test_hypothesis_generator_returns_full_records():
    batch = HypothesisDraftBatch(
        hypotheses=[
            _hypothesis_draft("first hypothesis"),
            _hypothesis_draft("second hypothesis"),
        ]
    )
    client = _FakeClient(parsed_output=batch)
    gen = AnthropicHypothesisGenerator(client=client)
    out = gen.generate(seed="", context=_ctx(), n=5)
    assert len(out) == 2
    assert all(isinstance(h, Hypothesis) for h in out)
    # Bookkeeping filled in
    for h in out:
        assert h.id.startswith("hyp_")
        assert h.status == "proposed"
        assert h.source == "llm_brainstorm"
        assert h.author  # non-empty


def test_hypothesis_generator_truncates_to_n():
    batch = HypothesisDraftBatch(
        hypotheses=[
            _hypothesis_draft(f"hypothesis number {i}") for i in range(5)
        ]
    )
    gen = AnthropicHypothesisGenerator(client=_FakeClient(parsed_output=batch))
    out = gen.generate(seed="", context=_ctx(), n=2)
    assert len(out) == 2


def test_hypothesis_generator_n_zero_short_circuits():
    client = _FakeClient(parsed_output=HypothesisDraftBatch(hypotheses=[]))
    gen = AnthropicHypothesisGenerator(client=client)
    out = gen.generate(seed="", context=_ctx(), n=0)
    assert out == []
    assert client.messages.last_call_kwargs is None  # no API call


def test_hypothesis_generator_records_cost():
    batch = HypothesisDraftBatch(hypotheses=[_hypothesis_draft()])
    gen = AnthropicHypothesisGenerator(client=_FakeClient(parsed_output=batch))
    gen.generate(seed="", context=_ctx(), n=1)
    assert gen.last_cost is not None
    assert gen.last_response_usage is not None


def test_hypothesis_prompt_includes_cache_control_block():
    batch = HypothesisDraftBatch(hypotheses=[_hypothesis_draft()])
    client = _FakeClient(parsed_output=batch)
    gen = AnthropicHypothesisGenerator(client=client)
    gen.generate(seed="VIX regime gating", context=_ctx(), n=1)

    kwargs = client.messages.last_call_kwargs
    assert kwargs["model"] == "claude-opus-4-7"
    assert kwargs["thinking"] == {"type": "adaptive"}
    # System is a list with two text blocks; second has cache_control.
    sys_blocks = kwargs["system"]
    assert len(sys_blocks) == 2
    assert "cache_control" in sys_blocks[1]
    assert sys_blocks[1]["cache_control"]["type"] == "ephemeral"
    # Registry IDs appear in the cached block.
    assert "vwap_reversion" in sys_blocks[1]["text"]
    assert "mes_vwap" in sys_blocks[1]["text"]


def test_hypothesis_prompt_user_carries_seed():
    batch = HypothesisDraftBatch(hypotheses=[_hypothesis_draft()])
    client = _FakeClient(parsed_output=batch)
    gen = AnthropicHypothesisGenerator(client=client)
    gen.generate(seed="post-FOMC drift studies", context=_ctx(), n=1)

    [user_msg] = client.messages.last_call_kwargs["messages"]
    assert user_msg["role"] == "user"
    assert "post-FOMC drift studies" in user_msg["content"]


def test_hypothesis_prompt_user_handles_empty_seed():
    batch = HypothesisDraftBatch(hypotheses=[_hypothesis_draft()])
    client = _FakeClient(parsed_output=batch)
    gen = AnthropicHypothesisGenerator(client=client)
    gen.generate(seed="", context=_ctx(), n=1)
    [user_msg] = client.messages.last_call_kwargs["messages"]
    # Should still produce a coherent prompt without seed text.
    assert "Generate" in user_msg["content"]


# ── Variant generator ────────────────────────────────────────


def _promoted_hypothesis() -> Hypothesis:
    return Hypothesis(
        id="hyp_test",
        title="t", source="human",
        created_date=date(2026, 5, 1),
        last_modified_date=date(2026, 5, 1),
        author="d",
        status="promoted",
        observation="o",
        mechanism="m",
        falsification="f",
        instrument_scope=["MES"],
        parameter_envelope=[
            ParameterRangeSpec(name="threshold", min=0.001, max=0.01),
        ],
    )


def test_variant_generator_returns_validated_specs():
    batch = StrategySpecBatch(specs=[_strategy_spec("v_a"), _strategy_spec("v_b")])
    gen = AnthropicVariantGenerator(client=_FakeClient(parsed_output=batch))
    out = gen.generate(hypothesis=_promoted_hypothesis(), context=_ctx(), n=5)
    assert len(out) == 2
    assert all(isinstance(s, StrategySpec) for s in out)


def test_variant_generator_stamps_author_and_dates():
    batch = StrategySpecBatch(specs=[_strategy_spec("v_a")])
    gen = AnthropicVariantGenerator(client=_FakeClient(parsed_output=batch), author_label="custom")
    [s] = gen.generate(hypothesis=_promoted_hypothesis(), context=_ctx(), n=1)
    assert s.metadata.author == "custom"
    # created_date / last_modified_date overwritten to today
    assert s.metadata.created_date == s.metadata.last_modified_date


def test_variant_generator_truncates_to_n():
    batch = StrategySpecBatch(
        specs=[_strategy_spec(f"v_{i}") for i in range(5)]
    )
    gen = AnthropicVariantGenerator(client=_FakeClient(parsed_output=batch))
    out = gen.generate(hypothesis=_promoted_hypothesis(), context=_ctx(), n=2)
    assert len(out) == 2


def test_variant_generator_n_zero_short_circuits():
    client = _FakeClient(parsed_output=StrategySpecBatch(specs=[]))
    gen = AnthropicVariantGenerator(client=client)
    out = gen.generate(hypothesis=_promoted_hypothesis(), context=_ctx(), n=0)
    assert out == []
    assert client.messages.last_call_kwargs is None


def test_variant_prompt_includes_hypothesis_fields():
    batch = StrategySpecBatch(specs=[_strategy_spec("v_a")])
    client = _FakeClient(parsed_output=batch)
    gen = AnthropicVariantGenerator(client=client)
    gen.generate(hypothesis=_promoted_hypothesis(), context=_ctx(), n=1)

    [user_msg] = client.messages.last_call_kwargs["messages"]
    body = user_msg["content"]
    assert "Mechanism: m" in body
    assert "Falsification: f" in body
    assert "threshold" in body
    assert "0.001" in body and "0.01" in body


def test_variant_prompt_caches_registry():
    batch = StrategySpecBatch(specs=[_strategy_spec("v_a")])
    client = _FakeClient(parsed_output=batch)
    gen = AnthropicVariantGenerator(client=client)
    gen.generate(hypothesis=_promoted_hypothesis(), context=_ctx(), n=1)

    sys_blocks = client.messages.last_call_kwargs["system"]
    # Cache marker on the registry block (last system block).
    assert "cache_control" in sys_blocks[-1]


def test_variant_generator_records_cost():
    batch = StrategySpecBatch(specs=[_strategy_spec("v_a")])
    gen = AnthropicVariantGenerator(client=_FakeClient(parsed_output=batch))
    gen.generate(hypothesis=_promoted_hypothesis(), context=_ctx(), n=1)
    assert gen.last_cost is not None
