"""LLM session-labeling pipeline (Phase 2 of the regime classifier).

Mirrors the cache-discipline pattern from
`auto_generation/anthropic_generators.py`:
  * stable system frame (instructions + how-to-think)
  * cached system block (output schema + 3-5 few-shot examples)
  * per-call user message (a batch of N session snapshots to label)

The LLM emits a `SessionLabelBatch`; we validate it with Pydantic,
unpack into per-session `SessionLabel`s, and write each as its own
JSON file under `data/session_labels/<instrument>/<date>.json`.

Ensembling: the CALLER (CLI) invokes the generator multiple times
with the SAME session batch but different invocations of `generate`,
then aggregates by majority-vote on `regime_label` per session and
mean of `regime_confidence`. Stability across the ensemble is the
primary classifier-quality signal.

Cost discipline: cost is logged after every call (never gates).
Default model is Sonnet 4.6 — Opus is overkill for a classification
task that we'll distill anyway, and Sonnet 4.6 is ~4× cheaper.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import ValidationError

from tradegy.auto_generation.cost import (
    CostEstimate,
    cost_for_usage,
    format_cost_line,
)
from tradegy.regime.session_inputs import (
    SessionPreOpenSnapshot,
    format_snapshot_for_llm,
)
from tradegy.regime.session_labels import (
    SessionLabel,
    SessionLabelBatch,
)


_log = logging.getLogger(__name__)
DEFAULT_MODEL = "claude-sonnet-4-6"

# ── system-prompt blocks ────────────────────────────────────────────


_LABELING_FRAME = """\
You are a regime classifier for an intraday trading-strategy backtest.
For each trading session described in the user message, emit a
structured `SessionLabel` containing:

  * regime_label: one of {range, trend_up, trend_down, news_driven, uncertain}.
  * regime_confidence: 0.0-1.0. 0.5+ means you'd commit to this call.
  * regime_signals: 1-4 short tags naming WHICH inputs drove the call
    (e.g., "low_vix_pctile", "fomc_today", "outside_5d_range",
    "negative_overnight_gap").
  * news_events: per-scheduled-event impact assessment. ONLY include events
    listed in the input snapshot. For each event emit:
      - market_impact: minimal / transient / regime_changing.
      - halt_recommended_window_minutes: how long to halt entries after
        the event (0 if minimal; typically 30-180 for transient,
        120-240 for regime_changing).
  * reasoning: 1-3 sentences describing the call. Concrete inputs only;
    no boilerplate.

HARD CONSTRAINTS:

1. ONLY use the data in the snapshot. Do NOT use your knowledge of what
   actually happened on or after this date. Sealed-input rule: the
   classifier must be reproducible from the same inputs alone.

2. If signals contradict (e.g., low VIX percentile but FOMC statement
   today), bias toward the higher-impact signal — typically the news
   signal. The strategy this trains will fade range-bound sessions, so
   we'd rather over-label `news_driven` (false-positive halt) than
   miss a regime-changing event.

3. `uncertain` is a valid call when the inputs really are ambiguous
   (e.g., quiet pre-open with no scheduled events, neutral VIX, no
   meaningful 5d trend). Do not force a low-confidence regime call.

4. Confidence calibration: if you'd be 70% sure of the call after
   seeing the inputs, emit confidence 0.7. Don't anchor on round
   numbers.
"""


_SYSTEM_SCHEMA_TEMPLATE = """\
## Output schema

Respond with a single JSON object matching this Pydantic schema,
wrapped in a ```json code fence and nothing else:

```json-schema
{schema_json}
```

The `labels` array MUST have one entry per session in the input batch,
in the SAME ORDER. session_date and instrument fields must echo the
input exactly.
"""


_FEW_SHOT_TEMPLATE = """\
## Few-shot examples (for calibration)

### Example 1 — clear range day, low vol, no events

Snapshot:
  date: 2024-08-13
  instrument: MES
  today open: 5455.00
  prior close: 5440.50
  overnight gap: +0.27%
  prior-5d close-to-close: [+0.21%, -0.13%, +0.05%, +0.08%, -0.10%]
    (most-recent first)
  VIX at prior close: 16.45
  VIX 252-day percentile: 0.32
  VIX 5-day change: -0.85 pts
  scheduled events today: (none)

Output:
{{
  "labels": [{{
    "session_date": "2024-08-13",
    "instrument": "MES",
    "regime_label": "range",
    "regime_confidence": 0.78,
    "regime_signals": [
      "low_vix_pctile_0.32",
      "tight_5d_range",
      "no_scheduled_events"
    ],
    "news_events": [],
    "reasoning": "Low VIX percentile (0.32) plus tight prior-5d band (≤0.21%) and no scheduled events fit a range-bound profile. Small overnight gap (+0.27%) doesn't change the read."
  }}]
}}

### Example 2 — FOMC statement day, news_driven

Snapshot:
  date: 2024-09-18
  instrument: MES
  today open: 5638.00
  prior close: 5639.50
  overnight gap: -0.03%
  prior-5d close-to-close: [+0.42%, +0.16%, -0.21%, +0.09%, +0.31%]
  VIX at prior close: 17.50
  VIX 252-day percentile: 0.40
  VIX 5-day change: +0.30 pts
  scheduled events today (1):
    - 2024-09-18T18:00:00+00:00 [high] fomc_statement: Federal Reserve issues FOMC statement

Output:
{{
  "labels": [{{
    "session_date": "2024-09-18",
    "instrument": "MES",
    "regime_label": "news_driven",
    "regime_confidence": 0.92,
    "regime_signals": ["fomc_today", "high_importance_event"],
    "news_events": [{{
      "event_ts_utc": "2024-09-18T18:00:00Z",
      "event_type": "fomc_statement",
      "market_impact": "regime_changing",
      "halt_recommended_window_minutes": 240
    }}],
    "reasoning": "FOMC statement scheduled at 14:00 ET. Pre-event VIX 17.5 / pctile 0.40 is moderate, not depressed; expect both vol expansion AND directional move post-statement. Halt recommended through cash close."
  }}]
}}

### Example 3 — gap up + negative 5d momentum, uncertain

Snapshot:
  date: 2024-10-10
  instrument: MES
  today open: 5780.00
  prior close: 5750.00
  overnight gap: +0.52%
  prior-5d close-to-close: [-0.35%, -0.18%, +0.08%, -0.27%, -0.41%]
  VIX at prior close: 19.20
  VIX 252-day percentile: 0.55
  VIX 5-day change: +1.20 pts
  scheduled events today: (none)

Output:
{{
  "labels": [{{
    "session_date": "2024-10-10",
    "instrument": "MES",
    "regime_label": "uncertain",
    "regime_confidence": 0.45,
    "regime_signals": ["mixed_signals", "elevated_vix_change", "gap_up_into_downtrend"],
    "news_events": [],
    "reasoning": "Strong overnight gap up (+0.52%) into a 5d downtrend (-1.13% cumulative) creates conflicting setup. Rising VIX (+1.20 5d) suggests vol expansion. Neither clean range nor clear trend; safest to label uncertain."
  }}]
}}
"""


# ── prompt assembly ──────────────────────────────────────────────────


def _build_system_blocks() -> list[dict]:
    schema_json = json.dumps(SessionLabelBatch.model_json_schema(), indent=2)
    return [
        {"type": "text", "text": _LABELING_FRAME},
        {
            "type": "text",
            "text": _SYSTEM_SCHEMA_TEMPLATE.format(schema_json=schema_json),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": _FEW_SHOT_TEMPLATE,
        },
    ]


def _build_user_message(snapshots: list[SessionPreOpenSnapshot]) -> str:
    if not snapshots:
        raise ValueError("at least one snapshot required per call")
    blocks = []
    blocks.append(
        f"Label the following {len(snapshots)} session(s) per the schema. "
        "Order MUST match input order; emit one entry per session."
    )
    blocks.append("")
    for s in snapshots:
        blocks.append(format_snapshot_for_llm(s))
        blocks.append("")
    return "\n".join(blocks).rstrip() + "\n"


# ── JSON extraction ──────────────────────────────────────────────────


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json_blob(text: str) -> str:
    fenced = _FENCE_RE.findall(text)
    if fenced:
        # Largest fenced block wins.
        return max(fenced, key=len)
    # Fall back to the largest balanced object.
    if "{" not in text:
        raise ValueError("no JSON object found in response")
    start = text.index("{")
    return text[start:]


def _parse_batch(text: str) -> SessionLabelBatch:
    blob = _extract_json_blob(text)
    try:
        data = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise ValueError(f"response is not valid JSON: {exc}") from exc
    try:
        return SessionLabelBatch.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"response failed schema validation: {exc}") from exc


# ── generator ────────────────────────────────────────────────────────


class AnthropicSessionLabeler:
    """LLM-driven session-label generator.

    One `generate(snapshots)` call returns a list of `SessionLabel`s,
    one per snapshot, in input order. Tests inject a fake client.
    """

    id = "anthropic_session_labeler_v1"

    def __init__(
        self,
        *,
        client: Any,  # anthropic.Anthropic — duck-typed
        model: str = DEFAULT_MODEL,
        max_tokens: int = 16_000,
    ) -> None:
        self._client = client
        self._model = model
        self._max_tokens = max_tokens
        self.last_cost: CostEstimate | None = None
        self.last_response_usage: Any = None

    def generate(
        self, snapshots: list[SessionPreOpenSnapshot]
    ) -> list[SessionLabel]:
        if not snapshots:
            return []
        system_blocks = _build_system_blocks()
        user_text = _build_user_message(snapshots)

        response = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system=system_blocks,
            messages=[{"role": "user", "content": user_text}],
        )
        self._record_cost(response)

        text = "".join(
            b.text for b in response.content
            if getattr(b, "type", None) == "text"
        )
        batch = _parse_batch(text)
        if len(batch.labels) != len(snapshots):
            raise ValueError(
                f"LLM returned {len(batch.labels)} labels for "
                f"{len(snapshots)} snapshots — order or count mismatch"
            )
        return list(batch.labels)

    def _record_cost(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        self.last_response_usage = usage
        if usage is not None:
            self.last_cost = cost_for_usage(self._model, usage)
            _log.info("session-label cost: %s", format_cost_line(self.last_cost))
