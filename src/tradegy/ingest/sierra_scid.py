"""Stage 1 — Sierra Chart SCID ingest for VX futures.

Sierra Chart stores intraday chart data in binary ``.scid`` files. The files
downloaded for VX are per contract month, so this ingest path performs three
explicit steps:

1. parse each matching ``VX*_FUT_CFE.scid`` file,
2. aggregate records to 1-minute OHLCV bars,
3. collapse overlapping contract months into a non-back-adjusted front-month
   continuous series by choosing the earliest listed contract available at each
   minute.

The roll rule does not use future volume or returns. It uses only contract
month ordering and whether that contract has a bar at the timestamp.
"""
from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import numpy as np
import polars as pl

from tradegy.ingest._common import (
    IngestResult,
    now_utc_iso,
    source_root,
    write_date_partitions,
    write_receipt,
)
from tradegy.types import DataSource


_SCID_HEADER_SIZE = 56
_SCID_RECORD_SIZE = 40
_SCID_MAGIC = b"SCID"
_SC_EPOCH_UNIX_OFFSET_US = int(
    (datetime(1970, 1, 1, tzinfo=timezone.utc) - datetime(1899, 12, 30, tzinfo=timezone.utc)).total_seconds()
    * 1_000_000
)
_SCID_DTYPE = np.dtype(
    [
        ("sc_datetime_us", "<i8"),
        ("open", "<f4"),
        ("high", "<f4"),
        ("low", "<f4"),
        ("close", "<f4"),
        ("num_trades", "<u4"),
        ("volume", "<u4"),
        ("bid_volume", "<u4"),
        ("ask_volume", "<u4"),
    ]
)
_VX_SCID_RE = re.compile(r"^VX([FGHJKMNQUVXZ])(\d{2})_FUT_CFE\.scid$")
_MONTH_CODE_TO_NUM = {
    "F": 1,
    "G": 2,
    "H": 3,
    "J": 4,
    "K": 5,
    "M": 6,
    "N": 7,
    "Q": 8,
    "U": 9,
    "V": 10,
    "X": 11,
    "Z": 12,
}


@dataclass(frozen=True)
class ScidContract:
    path: Path
    symbol: str
    year: int
    month: int

    @property
    def sort_key(self) -> int:
        return self.year * 100 + self.month


def ingest_vx_scid_directory(
    scid_dir: Path,
    source: DataSource,
    *,
    out_dir: Path | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> IngestResult:
    """Ingest Sierra Chart VX SCID files into a continuous 1-minute source.

    ``source.ingest.format`` must be ``sierra_chart_scid_vx``. If start/end
    dates are omitted, the registry coverage window is used. ``end_date`` is
    inclusive at the date level.
    """
    if not scid_dir.exists() or not scid_dir.is_dir():
        raise NotADirectoryError(scid_dir)
    if source.ingest is None or source.ingest.format != "sierra_chart_scid_vx":
        raise ValueError(
            f"source {source.id!r} is not declared as sierra_chart_scid_vx "
            "(ingest.format mismatch)"
        )

    start_date = start_date or source.coverage.start_date
    end_date = end_date or source.coverage.end_date
    if end_date < start_date:
        raise ValueError(f"end_date {end_date} precedes start_date {start_date}")
    start_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end_exclusive = datetime.combine(end_date, time.min, tzinfo=timezone.utc) + timedelta(days=1)

    contracts = _discover_vx_contracts(scid_dir)
    if not contracts:
        raise FileNotFoundError(f"no VX*_FUT_CFE.scid files under {scid_dir}")
    _assert_complete_months(contracts, start_date, end_date)

    minute_frames: list[pl.DataFrame] = []
    rows_in = 0
    contract_minutes_in = 0
    files_used: list[str] = []
    for contract in contracts:
        if contract.year < start_date.year - 1 or contract.year > end_date.year + 1:
            continue
        raw = _read_scid_records(contract.path)
        if raw.height == 0:
            continue
        raw_window_rows = raw.filter(
            (pl.col("ts_utc") >= start_dt) & (pl.col("ts_utc") < end_exclusive)
        ).height
        if raw_window_rows == 0:
            continue
        bars = _aggregate_contract_to_1m(raw, contract, start_dt, end_exclusive)
        if bars.height == 0:
            continue
        rows_in += raw_window_rows
        contract_minutes_in += bars.height
        minute_frames.append(bars)
        files_used.append(contract.path.name)

    if not minute_frames:
        raise ValueError(
            f"SCID files under {scid_dir} produced no 1m VX bars in "
            f"[{start_date}, {end_date}]"
        )

    all_contract_minutes = pl.concat(minute_frames).sort(["ts_utc", "contract_sort"])
    continuous = (
        all_contract_minutes.group_by("ts_utc", maintain_order=True)
        .agg(
            [
                pl.col("open").first(),
                pl.col("high").first(),
                pl.col("low").first(),
                pl.col("close").first(),
                pl.col("volume").first(),
                pl.col("num_trades").first(),
                pl.col("bid_volume").first(),
                pl.col("ask_volume").first(),
                pl.col("symbol").first(),
                pl.col("contract_year").first(),
                pl.col("contract_month").first(),
            ]
        )
        .sort("ts_utc")
    )
    rows_out = continuous.height
    if rows_out == 0:
        raise ValueError("front-month collapse produced zero rows")

    coverage_start = continuous.select(pl.col("ts_utc").min()).item()
    coverage_end = continuous.select(pl.col("ts_utc").max()).item()
    overlapping_contract_minutes_dropped = contract_minutes_in - rows_out

    out_root = source_root(source.id, out_dir=out_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    partitions_written = write_date_partitions(continuous, out_root)

    manifest_hash = _scid_manifest_hash([scid_dir / name for name in files_used])
    batch_id = _compute_scid_batch_id(source.id, source.version, manifest_hash)
    write_receipt(
        out_root,
        batch_id,
        {
            "source_id": source.id,
            "source_version": source.version,
            "batch_id": batch_id,
            "format": "sierra_chart_scid_vx",
            "scid_dir": str(scid_dir),
            "manifest_hash": manifest_hash,
            "ingested_at": now_utc_iso(),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "rows_in": rows_in,
            "contract_minutes_in": contract_minutes_in,
            "rows_out": rows_out,
            "overlapping_contract_minutes_dropped": overlapping_contract_minutes_dropped,
            "coverage_start": coverage_start.isoformat(),
            "coverage_end": coverage_end.isoformat(),
            "partitions": [str(p) for p in partitions_written],
            "files_used": files_used,
            "roll_method": "earliest_listed_contract_available_per_minute",
            "back_adjusted": False,
        },
    )

    return IngestResult(
        source_id=source.id,
        batch_id=batch_id,
        raw_path=out_root,
        rows_in=rows_in,
        rows_out=rows_out,
        duplicates_dropped=overlapping_contract_minutes_dropped,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        partitions_written=partitions_written,
    )


def _discover_vx_contracts(scid_dir: Path) -> list[ScidContract]:
    contracts: list[ScidContract] = []
    for path in scid_dir.glob("VX*_FUT_CFE.scid"):
        match = _VX_SCID_RE.match(path.name)
        if match is None:
            continue
        month_code, yy = match.groups()
        year = 2000 + int(yy)
        contracts.append(
            ScidContract(
                path=path,
                symbol=path.stem,
                year=year,
                month=_MONTH_CODE_TO_NUM[month_code],
            )
        )
    return sorted(contracts, key=lambda c: c.sort_key)


def _assert_complete_months(
    contracts: list[ScidContract], start_date: date, end_date: date
) -> None:
    available = {(c.year, c.month) for c in contracts}
    expected: list[tuple[int, int]] = []
    year = start_date.year
    month = start_date.month
    while (year, month) <= (end_date.year, end_date.month):
        expected.append((year, month))
        month += 1
        if month == 13:
            year += 1
            month = 1
    missing = [ym for ym in expected if ym not in available]
    if missing:
        rendered = ", ".join(f"{y}-{m:02d}" for y, m in missing)
        raise FileNotFoundError(f"missing VX SCID contract months: {rendered}")


def _read_scid_records(path: Path) -> pl.DataFrame:
    with path.open("rb") as f:
        header = f.read(_SCID_HEADER_SIZE)
    if len(header) != _SCID_HEADER_SIZE:
        raise ValueError(f"{path.name}: incomplete SCID header")
    magic, header_size, record_size, version, _unused1, _utc_start = struct.unpack(
        "<4sIIHHI", header[:20]
    )
    if magic != _SCID_MAGIC:
        raise ValueError(f"{path.name}: bad SCID magic {magic!r}")
    if header_size != _SCID_HEADER_SIZE:
        raise ValueError(f"{path.name}: unexpected SCID header size {header_size}")
    if record_size != _SCID_RECORD_SIZE:
        raise ValueError(f"{path.name}: unexpected SCID record size {record_size}")
    if version != 1:
        raise ValueError(f"{path.name}: unsupported SCID version {version}")

    size = path.stat().st_size
    payload = size - header_size
    if payload < 0 or payload % record_size != 0:
        raise ValueError(f"{path.name}: file size is not aligned to SCID records")
    if payload == 0:
        return pl.DataFrame(
            schema={
                "ts_utc": pl.Datetime("us", "UTC"),
                "open": pl.Float64,
                "high": pl.Float64,
                "low": pl.Float64,
                "close": pl.Float64,
                "num_trades": pl.Int64,
                "volume": pl.Int64,
                "bid_volume": pl.Int64,
                "ask_volume": pl.Int64,
            }
        )

    arr = np.fromfile(path, dtype=_SCID_DTYPE, offset=header_size)
    unix_us = arr["sc_datetime_us"].astype("int64") - _SC_EPOCH_UNIX_OFFSET_US
    return pl.DataFrame(
        {
            "ts_utc": unix_us,
            "open": arr["open"].astype("float64"),
            "high": arr["high"].astype("float64"),
            "low": arr["low"].astype("float64"),
            "close": arr["close"].astype("float64"),
            "num_trades": arr["num_trades"].astype("int64"),
            "volume": arr["volume"].astype("int64"),
            "bid_volume": arr["bid_volume"].astype("int64"),
            "ask_volume": arr["ask_volume"].astype("int64"),
        }
    ).with_columns(pl.col("ts_utc").cast(pl.Datetime("us", "UTC")))


def _aggregate_contract_to_1m(
    raw: pl.DataFrame,
    contract: ScidContract,
    start_dt: datetime,
    end_exclusive: datetime,
) -> pl.DataFrame:
    filtered = raw.filter(
        (pl.col("ts_utc") >= start_dt) & (pl.col("ts_utc") < end_exclusive)
    )
    if filtered.height == 0:
        return pl.DataFrame()

    is_tick_with_bid_ask = pl.col("open") == 0.0
    normalized = filtered.with_columns(
        [
            pl.col("ts_utc").dt.truncate("1m").alias("__minute"),
            pl.when(is_tick_with_bid_ask)
            .then(pl.col("close"))
            .otherwise(pl.col("open"))
            .alias("__open"),
            pl.when(is_tick_with_bid_ask)
            .then(pl.col("close"))
            .otherwise(pl.col("high"))
            .alias("__high"),
            pl.when(is_tick_with_bid_ask)
            .then(pl.col("close"))
            .otherwise(pl.col("low"))
            .alias("__low"),
        ]
    ).sort("ts_utc")

    return (
        normalized.group_by("__minute", maintain_order=True)
        .agg(
            [
                pl.col("__open").first().alias("open"),
                pl.col("__high").max().alias("high"),
                pl.col("__low").min().alias("low"),
                pl.col("close").last().alias("close"),
                pl.col("volume").sum().alias("volume"),
                pl.col("num_trades").sum().alias("num_trades"),
                pl.col("bid_volume").sum().alias("bid_volume"),
                pl.col("ask_volume").sum().alias("ask_volume"),
            ]
        )
        .rename({"__minute": "ts_utc"})
        .with_columns(
            [
                pl.lit(contract.symbol).alias("symbol"),
                pl.lit(contract.year).alias("contract_year"),
                pl.lit(contract.month).alias("contract_month"),
                pl.lit(contract.sort_key).alias("contract_sort"),
            ]
        )
        .select(
            [
                "ts_utc",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "num_trades",
                "bid_volume",
                "ask_volume",
                "symbol",
                "contract_year",
                "contract_month",
                "contract_sort",
            ]
        )
    )


def _scid_manifest_hash(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for path in sorted(paths):
        stat = path.stat()
        h.update(path.name.encode())
        h.update(b"\0")
        h.update(str(stat.st_size).encode())
        h.update(b"\0")
        with path.open("rb") as f:
            h.update(f.read(_SCID_HEADER_SIZE + _SCID_RECORD_SIZE))
            if stat.st_size >= _SCID_RECORD_SIZE:
                f.seek(max(_SCID_HEADER_SIZE, stat.st_size - _SCID_RECORD_SIZE))
                h.update(f.read(_SCID_RECORD_SIZE))
        h.update(b"\0")
    return h.hexdigest()


def _compute_scid_batch_id(source_id: str, source_version: str, manifest_hash: str) -> str:
    h = hashlib.sha256()
    h.update(source_id.encode())
    h.update(b"\0")
    h.update(source_version.encode())
    h.update(b"\0")
    h.update(manifest_hash.encode())
    return h.hexdigest()[:16]
