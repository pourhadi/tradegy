"""Post-call cost reporting for Anthropic API calls.

Format `response.usage` (or any `BetaUsage` / `Usage`) into a single
human-readable line for CLI output. Pricing per million tokens is
keyed by the bare model name; lookups for unknown models return zeros
with a note rather than raising.

We do NOT gate calls on cost — the pipeline runs the API call
directly and surfaces the actual cost after the fact. This module
exists for visibility, not enforcement.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# Per `shared/models.md` (cached 2026-04-15) and the claude-api skill
# pricing table. USD per 1,000,000 tokens.
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-7":   (5.00, 25.00),
    "claude-opus-4-6":   (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5":  (1.00, 5.00),
}


@dataclass(frozen=True)
class CostEstimate:
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    estimated_usd: float
    note: str = ""


def cost_for_usage(model: str, usage: Any) -> CostEstimate:
    """Compute an approximate USD cost from an Anthropic usage object.

    `usage` is whatever the SDK returns on `response.usage` — duck-typed
    for the four fields we care about. Cache reads are billed at ~10% of
    input price; cache writes (5-min TTL) at ~125%. The rough model
    matches the rates in the `claude-api` skill — for governance-grade
    accounting use the live invoice from console.anthropic.com instead.
    """
    in_tok = int(getattr(usage, "input_tokens", 0) or 0)
    out_tok = int(getattr(usage, "output_tokens", 0) or 0)
    cache_create = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)

    rates = _PRICING.get(model)
    if rates is None:
        return CostEstimate(
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_creation_input_tokens=cache_create,
            cache_read_input_tokens=cache_read,
            estimated_usd=0.0,
            note=f"unknown model {model!r} — cost not estimated",
        )
    in_rate, out_rate = rates
    cost = (
        in_tok / 1_000_000 * in_rate
        + cache_create / 1_000_000 * in_rate * 1.25
        + cache_read / 1_000_000 * in_rate * 0.10
        + out_tok / 1_000_000 * out_rate
    )
    return CostEstimate(
        model=model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_creation_input_tokens=cache_create,
        cache_read_input_tokens=cache_read,
        estimated_usd=cost,
    )


def format_cost_line(est: CostEstimate) -> str:
    parts = [
        f"in={est.input_tokens:,}",
        f"out={est.output_tokens:,}",
    ]
    if est.cache_creation_input_tokens or est.cache_read_input_tokens:
        parts.append(
            f"cache(write={est.cache_creation_input_tokens:,}, "
            f"read={est.cache_read_input_tokens:,})"
        )
    parts.append(f"~${est.estimated_usd:.4f}")
    if est.note:
        parts.append(f"({est.note})")
    return f"[{est.model}] " + "  ".join(parts)
