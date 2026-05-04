"""Pydantic schemas for LLM session labels.

The LLM is asked to emit a `SessionLabel` per (session_date,
instrument). It contains a regime classification (range / trend_up /
trend_down / news_driven / uncertain), confidence, the signals the
classifier keyed on, per-event impact assessments, and short
reasoning text for audit.

These schemas are the shape we ask Claude to emit AND the shape the
distillation classifier is trained against. The two are coupled
deliberately — if the schema changes, both layers regenerate.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Allowed regime labels. Five classes balance enough discrimination
# (range vs trend vs news-driven) without forcing the LLM to over-
# resolve in ambiguous sessions (we let it say "uncertain").
RegimeLabel = Literal[
    "range",        # session is range-bound; mean-reversion strategies favored
    "trend_up",     # sustained directional drift up
    "trend_down",   # sustained directional drift down
    "news_driven",  # event-driven, regime distorted by scheduled or breaking news
    "uncertain",    # LLM could not call it confidently; bias toward not deploying
]


# Per-event market-impact assessment. The LLM rates each scheduled or
# observable event for the day. `regime_changing` events should HALT
# strategy entries; `transient` events suggest a brief volatility burst
# but don't change the day's character.
NewsImpact = Literal["minimal", "transient", "regime_changing"]


class LabeledNewsEvent(_Strict):
    event_ts_utc: datetime = Field(
        description="UTC timestamp of the event (matches econ_events.ts_utc).",
    )
    event_type: str = Field(
        description="Short event-type tag (e.g., fomc_statement, cpi).",
    )
    market_impact: NewsImpact
    halt_recommended_window_minutes: int = Field(
        ge=0, le=480,
        description="How many minutes after the event to halt new entries. "
                    "0 if no halt; otherwise typically 30-240.",
    )


class SessionLabel(_Strict):
    """A single labeled trading session for a single instrument.

    Sealed-input promise: the inputs handed to the LLM contained NO
    data after 09:30 ET on `session_date`. Anything after open is
    out-of-sample by construction.
    """

    session_date: date
    instrument: Literal["MES", "SPY", "ES"] = Field(
        description="Instrument the session label applies to.",
    )
    regime_label: RegimeLabel
    regime_confidence: float = Field(ge=0.0, le=1.0)
    regime_signals: list[str] = Field(
        default_factory=list,
        description="Which input signals drove the regime call (free-text "
                    "tags; used for auditing the classifier's reasoning).",
    )
    news_events: list[LabeledNewsEvent] = Field(
        default_factory=list,
        description="Per-event impact assessment for the day's scheduled "
                    "events. Empty if no scheduled events today.",
    )
    reasoning: str = Field(
        max_length=2000,
        description="Short audit note explaining the call (1-3 sentences).",
    )


class SessionLabelBatch(_Strict):
    """Batch wrapper for one labeling call. The LLM emits one batch per
    invocation; we then index sessions individually from it."""

    labels: list[SessionLabel]
