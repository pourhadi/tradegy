"""Schema-level checks on DataSource, IngestSpec, LiveSpec.

The DataSource model is the registration boundary for the live/historical
parity contract: every source pairs an `ingest` adapter with a `live`
adapter. The validator must enforce timestamp-declaration discipline so
ingest paths can rely on exactly one source-of-truth for the timestamp.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from tradegy.types import (
    AvailabilityLatency,
    Coverage,
    DataSource,
    FieldSpec,
    IngestSpec,
    LiveSpec,
)


def _base_kwargs(**overrides):
    base = dict(
        id="test_src",
        version="v1",
        description="x",
        type="market_data",
        provider="local_csv",
        revisable=False,
        revision_policy="never_revised",
        admission_rationale="x",
        coverage=Coverage(start_date="2020-01-01", end_date="2020-01-02"),
        cadence="1s",
        fields=[FieldSpec(name="ts", type="timestamp")],
        timestamp_column="ts",
        availability_latency=AvailabilityLatency(median_seconds=0.0, p99_seconds=0.0),
    )
    base.update(overrides)
    return base


def test_generic_source_with_timestamp_column_validates() -> None:
    src = DataSource(**_base_kwargs())
    assert src.timestamp_column == "ts"
    assert src.ingest is None
    assert src.live is None


def test_multi_column_via_ingest_spec_validates() -> None:
    src = DataSource(
        **_base_kwargs(
            timestamp_column=None,
            ingest=IngestSpec(
                format="sierra_chart_csv",
                timestamp_columns=["Date", "Time"],
                timestamp_format="%Y/%-m/%-d %H:%M:%S",
                column_remap={"Last": "close"},
            ),
        )
    )
    assert src.ingest is not None
    assert src.ingest.timestamp_columns == ["Date", "Time"]


def test_both_declared_rejects() -> None:
    with pytest.raises(ValidationError):
        DataSource(
            **_base_kwargs(
                ingest=IngestSpec(
                    format="sierra_chart_csv",
                    timestamp_columns=["Date", "Time"],
                ),
            )
        )


def test_neither_declared_rejects() -> None:
    with pytest.raises(ValidationError):
        DataSource(**_base_kwargs(timestamp_column=None))


def test_live_spec_round_trips() -> None:
    src = DataSource(
        **_base_kwargs(
            live=LiveSpec(
                adapter="ibkr_realtime_bars_5s",
                params={"symbol": "MES", "exchange": "CME"},
            ),
        )
    )
    assert src.live is not None
    assert src.live.adapter == "ibkr_realtime_bars_5s"
    assert src.live.params["symbol"] == "MES"
