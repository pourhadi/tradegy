"""Pydantic models mirroring registry YAML schemas (per 02_feature_pipeline.md).

These are the canonical in-memory shapes for the data-source and feature
registries. Fields and naming track the YAML examples in
trading_platform_docs/02_feature_pipeline.md sections "Data source registry
schema" and "Feature registry schema" so registry files can be deserialized
without translation.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RevisionPolicy = Literal["never_revised", "revised_with_vintages", "not_admitted"]
Derivation = Literal["raw", "transform", "model"]
SourceType = Literal["market_data", "economic", "news", "alternative", "derived"]
LifecycleState = Literal["in_development", "research", "live", "deprecated", "retired"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldSpec(_Strict):
    name: str
    type: str


class Coverage(_Strict):
    start_date: date
    end_date: date
    gaps: list[dict[str, Any]] = Field(default_factory=list)


class AvailabilityLatency(_Strict):
    median_seconds: float
    p99_seconds: float
    notes: str = ""


class Licensing(_Strict):
    live_use: bool = True
    backtest_use: bool = True
    redistribution: bool = False


class AuditEntry(_Strict):
    date: date
    auditor: str
    result: str
    notes: str = ""


IngestFormat = Literal["generic_csv", "sierra_chart_csv"]


class IngestSpec(_Strict):
    """How a historical CSV (or future bulk file) maps onto the canonical
    raw schema for a source.

    A `format` discriminator picks the parser; the remaining fields
    parameterize that parser. Per 02_feature_pipeline.md, each source must
    declare a format explicitly — formats are not inferred from headers.
    """

    format: IngestFormat
    timestamp_columns: list[str] | None = None
    timestamp_format: str | None = None
    column_remap: dict[str, str] = Field(default_factory=dict)


class LiveSpec(_Strict):
    """Binding to a registered live adapter that produces the same canonical
    row schema as the historical ingest path.

    `adapter` is the name a live adapter is registered under via
    `register_live_adapter`. `params` is forwarded to the adapter at
    `subscribe(...)` time (e.g., contract specifier, useRTH).
    """

    adapter: str
    params: dict[str, Any] = Field(default_factory=dict)


class DataSource(_Strict):
    id: str
    version: str
    description: str
    type: SourceType
    provider: str
    revisable: bool
    revision_policy: RevisionPolicy
    admission_rationale: str
    coverage: Coverage
    cadence: str
    fields: list[FieldSpec]
    timestamp_column: str | None = None
    availability_latency: AvailabilityLatency
    licensing: Licensing = Field(default_factory=Licensing)
    known_issues: list[str] = Field(default_factory=list)
    audit_history: list[AuditEntry] = Field(default_factory=list)
    ingest: IngestSpec | None = None
    live: LiveSpec | None = None
    session_calendar: str | None = None
    max_inactivity_seconds: float | None = None

    @model_validator(mode="after")
    def _check_timestamp_declaration(self) -> "DataSource":
        # Exactly one of timestamp_column or ingest.timestamp_columns must be set.
        has_single = self.timestamp_column is not None
        has_multi = (
            self.ingest is not None and self.ingest.timestamp_columns is not None
        )
        if has_single and has_multi:
            raise ValueError(
                f"source {self.id!r}: declare either timestamp_column or "
                "ingest.timestamp_columns, not both"
            )
        if not (has_single or has_multi):
            raise ValueError(
                f"source {self.id!r}: must declare timestamp_column "
                "(generic) or ingest.timestamp_columns (multi-column)"
            )
        return self


class FeatureInput(_Strict):
    source_id: str | None = None
    feature_id: str | None = None
    resampled_to: str | None = None
    min_history_required: str | None = None


class Computation(_Strict):
    type: Literal["registered_transform"] = "registered_transform"
    transform_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class ExpectedRange(_Strict):
    min: float
    max: float


class ValidationRecord(_Strict):
    no_lookahead_audit: dict[str, Any] = Field(default_factory=dict)
    reproducibility: dict[str, Any] = Field(default_factory=dict)
    distribution_stability: dict[str, Any] = Field(default_factory=dict)


class Feature(_Strict):
    id: str
    version: str
    description: str
    type: Literal["raw", "derived", "model_backed"] = "derived"
    inputs: list[FeatureInput]
    computation: Computation
    cadence: str
    availability_latency_seconds: int
    derivation: Derivation
    revisable: bool
    expected_range: ExpectedRange
    outlier_policy: Literal["flag_and_pass", "drop", "fail"] = "flag_and_pass"
    historical_coverage: Coverage | None = None
    lifecycle_state: LifecycleState = "in_development"
    dependent_models: list[str] = Field(default_factory=list)
    dependent_strategies: list[str] = Field(default_factory=list)
    validation_record: ValidationRecord = Field(default_factory=ValidationRecord)


class AuditFinding(_Strict):
    severity: Literal["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
    code: str
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)


class AuditReport(_Strict):
    source_id: str
    batch_id: str
    generated_at: datetime
    row_count: int
    deduplicated_count: int
    coverage_start: datetime
    coverage_end: datetime
    findings: list[AuditFinding] = Field(default_factory=list)

    @property
    def has_critical(self) -> bool:
        return any(f.severity == "CRITICAL" for f in self.findings)
