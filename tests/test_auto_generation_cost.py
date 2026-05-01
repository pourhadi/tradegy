"""Cost-reporting helper tests."""
from __future__ import annotations

from dataclasses import dataclass

from tradegy.auto_generation.cost import cost_for_usage, format_cost_line


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


def test_known_model_costs_match_pricing_table():
    # 1M input + 1M output on opus 4.7 → $5 + $25 = $30
    est = cost_for_usage(
        "claude-opus-4-7",
        _Usage(input_tokens=1_000_000, output_tokens=1_000_000),
    )
    assert est.estimated_usd == 30.0


def test_cache_read_costs_10pct():
    est = cost_for_usage(
        "claude-opus-4-7",
        _Usage(input_tokens=0, output_tokens=0, cache_read_input_tokens=1_000_000),
    )
    # 1M cache_read at 10% of 5/M = $0.50
    assert abs(est.estimated_usd - 0.50) < 1e-9


def test_cache_write_costs_125pct():
    est = cost_for_usage(
        "claude-opus-4-7",
        _Usage(cache_creation_input_tokens=1_000_000),
    )
    # 1M cache_create at 125% of 5/M = $6.25
    assert abs(est.estimated_usd - 6.25) < 1e-9


def test_unknown_model_returns_zero_with_note():
    est = cost_for_usage(
        "imaginary-model", _Usage(input_tokens=1000),
    )
    assert est.estimated_usd == 0.0
    assert "imaginary-model" in est.note


def test_format_cost_line_includes_cache_when_present():
    est = cost_for_usage(
        "claude-opus-4-7",
        _Usage(
            input_tokens=100, output_tokens=50,
            cache_creation_input_tokens=1000,
            cache_read_input_tokens=2000,
        ),
    )
    s = format_cost_line(est)
    assert "claude-opus-4-7" in s
    assert "in=100" in s
    assert "out=50" in s
    assert "cache" in s


def test_format_cost_line_omits_cache_when_zero():
    est = cost_for_usage("claude-opus-4-7", _Usage(input_tokens=10))
    s = format_cost_line(est)
    assert "cache" not in s


def test_format_cost_line_includes_unknown_note():
    est = cost_for_usage("imaginary-model", _Usage(input_tokens=1))
    s = format_cost_line(est)
    assert "imaginary-model" in s
    assert "(" in s and ")" in s


def test_haiku_pricing():
    # Haiku 4.5: $1/$5 per 1M
    est = cost_for_usage(
        "claude-haiku-4-5",
        _Usage(input_tokens=1_000_000, output_tokens=1_000_000),
    )
    assert est.estimated_usd == 6.0
