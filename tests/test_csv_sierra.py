"""Sierra Chart OHLCV CSV ingest.

Builds a tiny CSV mirroring the Sierra Chart export shape (leading-space
headers, 'YYYY/M/D' single-digit month, OHLCV + signed-flow columns) and
verifies the canonical raw partition layout, column rename, timezone
conversion to UTC, and dedup behavior.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from tradegy.ingest._common import read_raw
from tradegy.ingest.csv_sierra import ingest_sierra_csv
from tradegy.types import (
    AvailabilityLatency,
    Coverage,
    DataSource,
    FieldSpec,
    IngestSpec,
    LiveSpec,
)


def _sierra_source(out_root: Path) -> DataSource:
    return DataSource(
        id="es_test",
        version="v1",
        description="test ES bars",
        type="market_data",
        provider="local_csv",
        revisable=False,
        revision_policy="never_revised",
        admission_rationale="x",
        coverage=Coverage(start_date=date(2019, 5, 6), end_date=date(2019, 5, 7)),
        cadence="1s",
        fields=[
            FieldSpec(name="ts_utc", type="timestamp"),
            FieldSpec(name="open", type="float"),
            FieldSpec(name="high", type="float"),
            FieldSpec(name="low", type="float"),
            FieldSpec(name="close", type="float"),
            FieldSpec(name="volume", type="int"),
            FieldSpec(name="num_trades", type="int"),
            FieldSpec(name="bid_volume", type="int"),
            FieldSpec(name="ask_volume", type="int"),
        ],
        availability_latency=AvailabilityLatency(median_seconds=0.0, p99_seconds=0.0),
        ingest=IngestSpec(
            format="sierra_chart_csv",
            timestamp_columns=["Date", "Time"],
        ),
        live=LiveSpec(
            adapter="ibkr_realtime_bars_5s",
            params={"symbol": "ES", "exchange": "CME"},
        ),
    )


def _write_sierra_fixture(path: Path) -> None:
    # Single-digit month and day, leading-space headers — mirrors Sierra
    # Chart's export verbatim. Two days so we exercise multi-day partitioning.
    rows = [
        ("2019/5/6", "13:30:00", 2898.00, 2898.50, 2896.00, 2896.00, 309, 60, 199, 110),
        ("2019/5/6", "13:30:05", 2895.75, 2895.75, 2894.00, 2894.50, 252, 65, 150, 102),
        ("2019/5/6", "13:30:10", 2894.50, 2895.00, 2894.00, 2894.75, 180, 40, 90, 90),
        # Duplicate timestamp — must be dedup'd.
        ("2019/5/6", "13:30:10", 2894.50, 2895.00, 2894.00, 2894.75, 180, 40, 90, 90),
        ("2019/5/7", "9:45:00", 2899.25, 2900.00, 2899.00, 2899.75, 410, 95, 220, 190),
    ]
    body = (
        "Date, Time, Open, High, Low, Last, Volume, NumberOfTrades, BidVolume, AskVolume\n"
        + "\n".join(", ".join(str(c) for c in r) for r in rows)
        + "\n"
    )
    path.write_text(body)


def test_ingest_writes_canonical_partitions(tmp_path: Path) -> None:
    csv_path = tmp_path / "es_sierra_fixture.csv"
    _write_sierra_fixture(csv_path)
    src = _sierra_source(tmp_path)

    out_root = tmp_path / "raw"
    result = ingest_sierra_csv(csv_path, src, input_tz="America/Chicago", out_dir=out_root)

    assert result.rows_in == 5
    assert result.rows_out == 4  # one duplicate dropped
    assert result.duplicates_dropped == 1
    assert len(result.partitions_written) == 2  # two distinct UTC dates


def test_ingest_round_trip_canonical_columns(tmp_path: Path) -> None:
    csv_path = tmp_path / "es_sierra_fixture.csv"
    _write_sierra_fixture(csv_path)
    src = _sierra_source(tmp_path)
    out_root = tmp_path / "raw"
    ingest_sierra_csv(csv_path, src, input_tz="America/Chicago", out_dir=out_root)

    df = read_raw("es_test", root=out_root)
    expected_cols = {
        "ts_utc",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "num_trades",
        "bid_volume",
        "ask_volume",
    }
    assert set(df.columns) == expected_cols
    # America/Chicago is UTC-5 in May (CDT). 13:30 CT == 18:30 UTC.
    first = df.row(0, named=True)
    assert first["ts_utc"].hour == 18
    assert first["ts_utc"].minute == 30
    assert df.schema["volume"] == pl.Int64
    assert df.schema["close"] == pl.Float64


def test_missing_columns_raises(tmp_path: Path) -> None:
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("Date, Time, Open, High\n2019/5/6, 13:30:00, 1, 2\n")
    src = _sierra_source(tmp_path)
    with pytest.raises(ValueError, match="missing expected columns"):
        ingest_sierra_csv(csv_path, src, input_tz="America/Chicago", out_dir=tmp_path)


def test_receipt_records_format_and_remap(tmp_path: Path) -> None:
    import json

    csv_path = tmp_path / "es_sierra_fixture.csv"
    _write_sierra_fixture(csv_path)
    src = _sierra_source(tmp_path)
    out_root = tmp_path / "raw"
    result = ingest_sierra_csv(csv_path, src, input_tz="America/Chicago", out_dir=out_root)

    receipt_path = out_root / "source=es_test" / "_receipts" / f"{result.batch_id}.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["format"] == "sierra_chart_csv"
    assert receipt["column_remap"]["Last"] == "close"
    assert receipt["streaming_chunk_years"] == 1
