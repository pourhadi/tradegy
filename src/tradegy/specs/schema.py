"""Pydantic models for the human-authored sections of a strategy spec.

Mirrors 04_strategy_spec_schema.md. Field names track the doc verbatim
so a YAML written against the doc deserializes without translation.

Harness-authored sections (`backtest_evidence`, `validation_record`,
`live_performance`) are deliberately omitted from this module — they're
written by the harness in append-only fashion and live in a separate
schema layer added when those features land.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


SpecStatus = Literal[
    "draft", "in_validation", "paper_trading", "live", "retired"
]
SpecTier = Literal["auto_execute", "confirm_then_execute", "proposal_only"]


class MetadataSpec(_Strict):
    id: str
    version: str
    schema_version: str = "1.0"
    name: str
    status: SpecStatus = "draft"
    created_date: date
    last_modified_date: date
    author: str
    reviewers: list[str] = Field(default_factory=list)
    parent_strategy_id: str | None = None
    description: str = ""


class TimeWindow(_Strict):
    start: str  # HH:MM
    end: str
    timezone: str


class BlackoutWindow(_Strict):
    type: str
    window_before_minutes: int = 0
    window_after_minutes: int = 0


class MarketScopeSpec(_Strict):
    market: str = "ES"
    instrument: str  # MES | ES
    session: Literal["RTH", "globex", "both"] = "globex"
    time_windows: list[TimeWindow] = Field(default_factory=list)
    blackout_dates: list[BlackoutWindow] = Field(default_factory=list)
    day_of_week_filter: list[str] = Field(default_factory=list)


class EntrySpec(_Strict):
    strategy_class: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    direction: Literal["long", "short", "both"] = "long"
    entry_order_type: Literal["limit", "market", "stop"] = "market"
    limit_offset_ticks: int = 0


class SizingSpec(_Strict):
    method: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class TimeStopBlock(_Strict):
    enabled: bool = False
    max_holding_bars: int = 0
    action_at_time_stop: str = "exit_market"


class StopsSpec(_Strict):
    initial_stop: dict[str, Any]  # {method: str, ...params}
    adjustment_rules: list[dict[str, Any]] = Field(default_factory=list)
    hard_max_distance_ticks: int = 1000
    time_stop: TimeStopBlock | None = None


class ProfitTarget(_Strict):
    method: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class InvalidationCondition(_Strict):
    condition: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    action: Literal["exit_market", "exit_limit", "tighten_stop"] = "exit_market"


class EndOfSessionSpec(_Strict):
    action: str = "flatten_before_close"
    minutes_before_close: int = 15


class ExitsSpec(_Strict):
    profit_targets: list[ProfitTarget] = Field(default_factory=list)
    invalidation_conditions: list[InvalidationCondition] = Field(default_factory=list)
    end_of_session: EndOfSessionSpec | None = None


class ParameterRange(_Strict):
    tested_min: float
    tested_max: float
    step: float | None = None


class ParameterEnvelopeSpec(_Strict):
    """Map of parameter name -> tested range. The validator enforces
    that every numeric parameter currently set in entry/sizing/stops/
    exits falls within its declared envelope, per docs:228-249.
    """

    model_config = ConfigDict(extra="allow")  # parameters vary per strategy

    def envelopes(self) -> dict[str, ParameterRange]:
        """Return all declared envelopes as ParameterRange instances.

        ``ConfigDict(extra="allow")`` stores YAML-supplied keys in
        ``model_extra`` rather than as fields, so iterate that.
        """
        out: dict[str, ParameterRange] = {}
        extras = self.model_extra or {}
        for name, value in extras.items():
            if isinstance(value, ParameterRange):
                out[name] = value
            elif isinstance(value, dict):
                out[name] = ParameterRange(**value)
        return out


class QuantitativeTrigger(_Strict):
    metric: str
    threshold: float
    action: Literal["flag_for_review", "auto_disable"]


class RetirementCriteriaSpec(_Strict):
    quantitative_triggers: list[QuantitativeTrigger] = Field(default_factory=list)
    qualitative_triggers: list[str] = Field(default_factory=list)
    minimum_trades_before_retirement_eligible: int = 20


class RiskEnvelopeSpec(_Strict):
    max_concurrent_instances: int = 1
    max_daily_loss_pct: float = 1.5
    max_weekly_loss_pct: float = 3.0


class OperationalSpec(_Strict):
    enabled: bool = True
    live_since: date | None = None
    risk_envelope: RiskEnvelopeSpec = Field(default_factory=RiskEnvelopeSpec)
    incompatible_with: list[str] = Field(default_factory=list)
    tier: SpecTier = "proposal_only"


class StrategySpec(_Strict):
    metadata: MetadataSpec
    market_scope: MarketScopeSpec
    entry: EntrySpec
    sizing: SizingSpec
    stops: StopsSpec
    exits: ExitsSpec
    parameter_envelope: ParameterEnvelopeSpec | None = None
    retirement_criteria: RetirementCriteriaSpec | None = None
    operational: OperationalSpec = Field(default_factory=OperationalSpec)
