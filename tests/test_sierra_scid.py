from __future__ import annotations

import struct
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from tradegy.ingest._common import read_raw
from tradegy.ingest.sierra_scid import ingest_scid_futures_directory, ingest_vx_scid_directory
from tradegy.types import (
    AvailabilityLatency,
    Coverage,
    DataSource,
    FieldSpec,
    IngestSpec,
)


_SC_EPOCH_UNIX_OFFSET_US = int(
    (datetime(1970, 1, 1, tzinfo=timezone.utc) - datetime(1899, 12, 30, tzinfo=timezone.utc)).total_seconds()
    * 1_000_000
)


def _vx_source() -> DataSource:
    return DataSource(
        id="vx_test",
        version="v1",
        description="test VX SCID source",
        type="market_data",
        provider="sierra_chart",
        revisable=False,
        revision_policy="never_revised",
        admission_rationale="test",
        coverage=Coverage(start_date=date(2024, 5, 1), end_date=date(2024, 6, 30)),
        cadence="1m",
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
            FieldSpec(name="symbol", type="string"),
            FieldSpec(name="contract_year", type="int"),
            FieldSpec(name="contract_month", type="int"),
        ],
        timestamp_column="ts_utc",
        ingest=IngestSpec(format="sierra_chart_scid_vx"),
        availability_latency=AvailabilityLatency(median_seconds=0, p99_seconds=0),
    )


def _nq_source() -> DataSource:
    return DataSource(
        id="nq_test",
        version="v1",
        description="test NQ SCID source",
        type="market_data",
        provider="sierra_chart",
        revisable=False,
        revision_policy="never_revised",
        admission_rationale="test",
        coverage=Coverage(start_date=date(2024, 5, 1), end_date=date(2024, 9, 30)),
        cadence="1m",
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
            FieldSpec(name="symbol", type="string"),
            FieldSpec(name="contract_year", type="int"),
            FieldSpec(name="contract_month", type="int"),
        ],
        timestamp_column="ts_utc",
        ingest=IngestSpec(
            format="sierra_chart_scid_futures",
            symbol_root="NQ",
            exchange="CME",
            contract_months="quarterly",
            filename_pattern="sierra_dash",
        ),
        availability_latency=AvailabilityLatency(median_seconds=0, p99_seconds=0),
    )


def _write_scid(path: Path, rows: list[tuple[datetime, float, float, float, float, int, int, int, int]]) -> None:
    header = struct.pack("<4sIIHHI36s", b"SCID", 56, 40, 1, 0, 0, b"\0" * 36)
    body = bytearray()
    for ts, open_, high, low, close, num_trades, volume, bid_volume, ask_volume in rows:
        sc_us = int(ts.timestamp() * 1_000_000) + _SC_EPOCH_UNIX_OFFSET_US
        body.extend(
            struct.pack(
                "<qffffIIII",
                sc_us,
                open_,
                high,
                low,
                close,
                num_trades,
                volume,
                bid_volume,
                ask_volume,
            )
        )
    path.write_bytes(header + body)


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_ingest_scid_aggregates_and_selects_front_month(tmp_path: Path) -> None:
    _write_scid(
        tmp_path / "VXK24_FUT_CFE.scid",
        [
            (_dt("2024-05-06T00:00:05"), 0.0, 10.05, 9.95, 10.0, 1, 1, 1, 0),
            (_dt("2024-05-06T00:00:45"), 0.0, 12.05, 11.95, 12.0, 1, 3, 0, 3),
            (_dt("2024-05-06T00:01:10"), 0.0, 13.05, 12.95, 13.0, 1, 2, 2, 0),
        ],
    )
    _write_scid(
        tmp_path / "VXM24_FUT_CFE.scid",
        [
            (_dt("2024-05-06T00:00:10"), 0.0, 20.05, 19.95, 20.0, 1, 5, 0, 5),
            (_dt("2024-05-06T00:02:00"), 21.0, 25.0, 20.0, 22.0, 4, 9, 4, 5),
        ],
    )

    result = ingest_vx_scid_directory(tmp_path, _vx_source(), out_dir=tmp_path / "raw")

    assert result.rows_in == 5
    assert result.rows_out == 3
    assert result.duplicates_dropped == 1

    df = read_raw("vx_test", root=tmp_path / "raw")
    first = df.row(0, named=True)
    assert first["symbol"] == "VXK24_FUT_CFE"
    assert first["open"] == pytest.approx(10.0)
    assert first["high"] == pytest.approx(12.0)
    assert first["low"] == pytest.approx(10.0)
    assert first["close"] == pytest.approx(12.0)
    assert first["volume"] == 4
    assert first["bid_volume"] == 1
    assert first["ask_volume"] == 3

    last = df.row(2, named=True)
    assert last["symbol"] == "VXM24_FUT_CFE"
    assert last["open"] == pytest.approx(21.0)
    assert last["high"] == pytest.approx(25.0)
    assert last["low"] == pytest.approx(20.0)
    assert last["close"] == pytest.approx(22.0)


def test_missing_required_contract_month_raises(tmp_path: Path) -> None:
    _write_scid(
        tmp_path / "VXK24_FUT_CFE.scid",
        [(_dt("2024-05-06T00:00:05"), 0.0, 10.05, 9.95, 10.0, 1, 1, 1, 0)],
    )

    with pytest.raises(FileNotFoundError, match="missing VX SCID contract months: 2024-06"):
        ingest_vx_scid_directory(tmp_path, _vx_source(), out_dir=tmp_path / "raw")


def test_ingest_quarterly_dash_scid_contracts(tmp_path: Path) -> None:
    _write_scid(
        tmp_path / "NQM24-CME.scid",
        [(_dt("2024-05-06T00:00:05"), -1.999e37, 18005.0, 0.0, 18002.0, 2, 4, 1, 3)],
    )
    _write_scid(
        tmp_path / "NQU24-CME.scid",
        [(_dt("2024-09-20T00:00:05"), 18100.0, 18105.0, 18095.0, 18102.0, 2, 5, 2, 3)],
    )

    result = ingest_scid_futures_directory(tmp_path, _nq_source(), out_dir=tmp_path / "raw")

    assert result.rows_in == 2
    assert result.rows_out == 2
    assert result.duplicates_dropped == 0

    df = read_raw("nq_test", root=tmp_path / "raw")
    first = df.row(0, named=True)
    assert first["symbol"] == "NQM24-CME"
    assert first["contract_month"] == 6
    assert first["open"] == pytest.approx(18002.0)
    assert first["high"] == pytest.approx(18005.0)
    assert first["low"] == pytest.approx(18002.0)
    assert first["close"] == pytest.approx(18002.0)


def test_missing_required_quarterly_contract_raises(tmp_path: Path) -> None:
    _write_scid(
        tmp_path / "NQM24-CME.scid",
        [(_dt("2024-05-06T00:00:05"), 18000.0, 18005.0, 17995.0, 18002.0, 2, 4, 1, 3)],
    )

    with pytest.raises(FileNotFoundError, match="missing NQ SCID contract months: 2024-09"):
        ingest_scid_futures_directory(tmp_path, _nq_source(), out_dir=tmp_path / "raw")
