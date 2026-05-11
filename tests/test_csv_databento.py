from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tradegy.ingest._common import read_raw
from tradegy.ingest.csv_databento import ingest_databento_csv
from tradegy.types import AvailabilityLatency, Coverage, DataSource, FieldSpec, IngestSpec


def _source() -> DataSource:
    return DataSource(
        id="zn_test",
        version="v1",
        description="test databento futures source",
        type="market_data",
        provider="databento",
        revisable=False,
        revision_policy="never_revised",
        admission_rationale="test",
        coverage=Coverage(start_date=date(2024, 1, 1), end_date=date(2024, 1, 2)),
        cadence="1m",
        fields=[
            FieldSpec(name="ts_utc", type="timestamp"),
            FieldSpec(name="open", type="float"),
            FieldSpec(name="high", type="float"),
            FieldSpec(name="low", type="float"),
            FieldSpec(name="close", type="float"),
            FieldSpec(name="volume", type="int"),
            FieldSpec(name="symbol", type="string"),
            FieldSpec(name="instrument_id", type="int"),
        ],
        ingest=IngestSpec(format="databento_ohlcv_csv", timestamp_columns=["ts_event"]),
        availability_latency=AvailabilityLatency(median_seconds=0, p99_seconds=0),
    )


def _write_csv(path: Path, rows: list[str]) -> None:
    path.write_text(
        "ts_event,rtype,publisher_id,instrument_id,open,high,low,close,volume,symbol\n"
        + "\n".join(rows)
        + "\n"
    )


def test_databento_ingest_excludes_calendar_spreads_before_front_roll(tmp_path: Path) -> None:
    csv_path = tmp_path / "zn.csv"
    _write_csv(
        csv_path,
        [
            "2024-01-01T00:00:00.000000000Z,33,1,1,100,101,99,100.5,10,ZNM4",
            "2024-01-01T00:00:00.000000000Z,33,1,2,-0.25,-0.25,-0.25,-0.25,999,ZNM4-ZNU4",
            "2024-01-02T00:00:00.000000000Z,33,1,1,101,102,100,101.5,5,ZNM4",
            "2024-01-02T00:00:00.000000000Z,33,1,2,-0.2,-0.2,-0.2,-0.2,999,ZNM4-ZNU4",
        ],
    )

    result = ingest_databento_csv(csv_path, _source(), out_dir=tmp_path / "raw")

    assert result.rows_in == 4
    assert result.rows_out == 2
    df = read_raw("zn_test", root=tmp_path / "raw")
    assert df["symbol"].to_list() == ["ZNM4", "ZNM4"]
    assert df["close"].to_list() == [100.5, 101.5]


def test_databento_ingest_requires_outright_futures(tmp_path: Path) -> None:
    csv_path = tmp_path / "spreads_only.csv"
    _write_csv(
        csv_path,
        ["2024-01-01T00:00:00.000000000Z,33,1,2,-0.25,-0.25,-0.25,-0.25,999,ZNM4-ZNU4"],
    )

    with pytest.raises(ValueError, match="contains no outright futures symbols"):
        ingest_databento_csv(csv_path, _source(), out_dir=tmp_path / "raw")
