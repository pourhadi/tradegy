"""databento options-on-futures CSV ingest tests.

Exercises the real on-disk X1A 2023-2024 MES options CSVs via the
`mes_options_x1a_csv_pair` fixture.  Per the no-synthetic-data rule
(see `~/.claude/projects/.../memory/feedback_no_synthetic_data.md`),
these tests do not use hand-built CSVs — they validate the parser
against the actual databento file shape.

Auto-marked `slow` via the `_SLOW_FIXTURES` hook in conftest.py
because the X1A definition CSV is ~75 MB.

Coverage:

  - `_parse_definitions` extracts only outright C/P contracts
    (multi-leg 'T' / 'M' rows excluded), deduplicates by
    instrument_id, and produces canonical strike/expiry/side/
    underlying columns.
  - `_parse_ohlcv` parses ts_event RFC3339+nanos to a UTC datetime
    column.
  - `_join_pair` filters out multi-leg combo bars (they have no
    matching C/P definition row) and tags the count via the
    `__combo_bars_filtered` column.
  - End-to-end `ingest_databento_options_pair` writes date-
    partitioned parquet, records the combo-bars count in the
    receipt, and produces a non-empty IngestResult.
"""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from tradegy.ingest.databento_options import (
    _join_pair,
    _parse_definitions,
    _parse_ohlcv,
    ingest_databento_options_pair,
)
from tradegy.options.chain import OptionSide
from tradegy.registry.loader import load_data_source


# ── Definition parsing ────────────────────────────────────────────


def test_parse_definitions_filters_to_outright_options_only(
    mes_options_x1a_csv_pair: dict[str, Path],
) -> None:
    """The raw 178k+ definition stream contains C, P, and T (multi-
    leg combo) rows. The parser should emit only outright C/P with
    side='call'/'put' as a string.
    """
    defs = _parse_definitions(mes_options_x1a_csv_pair["definition"])

    # Side column is normalized to the OptionSide enum's string values.
    sides = set(defs["side"].unique().to_list())
    assert sides == {"call", "put"}

    # No nulls in the canonical columns we use downstream.
    for col in ("instrument_id", "raw_symbol", "underlying", "expiry", "strike", "side"):
        n_null = defs[col].null_count()
        assert n_null == 0, f"unexpected nulls in {col}: {n_null}"


def test_parse_definitions_deduplicates_by_instrument_id(
    mes_options_x1a_csv_pair: dict[str, Path],
) -> None:
    """Each instrument_id should appear exactly once after parsing.
    The raw definition file has many rows per contract (one per
    update event); the parser keeps only the first.
    """
    defs = _parse_definitions(mes_options_x1a_csv_pair["definition"])
    n_unique = defs["instrument_id"].n_unique()
    assert len(defs) == n_unique


def test_parse_definitions_underlying_links_to_mes_quarterly(
    mes_options_x1a_csv_pair: dict[str, Path],
) -> None:
    """X1A options point at MES quarterly futures (MESH/M/U/Z<year>).
    The underlying column must be present and follow that pattern.
    """
    defs = _parse_definitions(mes_options_x1a_csv_pair["definition"])
    underlyings = set(defs["underlying"].unique().to_list())
    # Every underlying starts with 'MES' and ends with a quarterly
    # month code (H/M/U/Z) + a digit.
    for u in underlyings:
        assert u.startswith("MES"), f"unexpected underlying {u!r}"
        # MES + quarter-month-code + year-digit = 5 chars
        assert len(u) == 5
        assert u[3] in "HMUZ", f"non-quarterly month code in {u!r}"


# ── OHLCV parsing ─────────────────────────────────────────────────


def test_parse_ohlcv_emits_utc_timestamps(
    mes_options_x1a_csv_pair: dict[str, Path],
) -> None:
    """ts_event arrives as RFC3339 with trailing Z. The parser
    should produce a UTC-tz Datetime column on ts_utc.
    """
    bars = _parse_ohlcv(mes_options_x1a_csv_pair["ohlcv_1m"])
    schema = bars.schema
    # ts_utc must exist as a tz-aware Datetime in UTC.
    ts_dtype = schema["ts_utc"]
    assert isinstance(ts_dtype, pl.Datetime)
    assert ts_dtype.time_zone == "UTC", f"expected UTC tz, got {ts_dtype.time_zone}"


def test_parse_ohlcv_carries_canonical_columns(
    mes_options_x1a_csv_pair: dict[str, Path],
) -> None:
    bars = _parse_ohlcv(mes_options_x1a_csv_pair["ohlcv_1m"])
    expected = {"ts_utc", "instrument_id", "symbol", "open", "high", "low", "close", "volume"}
    assert set(bars.columns) == expected


# ── Join semantics ────────────────────────────────────────────────


def test_join_filters_combo_bars(
    mes_options_x1a_csv_pair: dict[str, Path],
) -> None:
    """Bars whose instrument_id corresponds to a multi-leg combo
    (instrument_class='T' in the raw definition file, e.g.
    `UD:EN: VT 2523136`) should be filtered out — backtests build
    combos from outright legs and never enter listed combos as a
    single instrument. The count should be tagged on every output
    row via __combo_bars_filtered for receipt-level audit.
    """
    bars_raw = pl.read_csv(
        mes_options_x1a_csv_pair["ohlcv_1m"],
        infer_schema_length=10000,
        ignore_errors=True,
    )
    n_bar_rows = len(bars_raw)

    joined = _join_pair(
        mes_options_x1a_csv_pair["definition"],
        mes_options_x1a_csv_pair["ohlcv_1m"],
    )
    n_joined = len(joined)
    # Every joined row has a non-null expiry (the filter target).
    assert joined["expiry"].null_count() == 0
    # Combo bars filtered, recorded on the metadata column.
    assert "__combo_bars_filtered" in joined.columns
    n_combo_filtered = int(joined["__combo_bars_filtered"].max())
    assert n_combo_filtered > 0, "expected SOME combo bars to be filtered"
    assert n_joined + n_combo_filtered == n_bar_rows


def test_join_emits_per_leg_metadata(
    mes_options_x1a_csv_pair: dict[str, Path],
) -> None:
    """Joined rows must carry strike, expiry, side, and underlying
    derived from the matched definition row.
    """
    joined = _join_pair(
        mes_options_x1a_csv_pair["definition"],
        mes_options_x1a_csv_pair["ohlcv_1m"],
    )
    sample = joined.head(50)
    for row in sample.iter_rows(named=True):
        # Side is in the OptionSide enum's string set.
        assert row["side"] in (OptionSide.CALL.value, OptionSide.PUT.value)
        # Strike is positive.
        assert row["strike"] > 0.0
        # Expiry is on or after the bar timestamp.
        bar_date = row["ts_utc"].date()
        assert row["expiry"] >= bar_date, (
            f"expiry {row['expiry']} predates bar date {bar_date}"
        )
        # Underlying is a 5-char MES quarterly code.
        assert row["underlying"].startswith("MES")


# ── End-to-end ingest ─────────────────────────────────────────────


def test_ingest_writes_date_partitions(
    mes_options_x1a_csv_pair: dict[str, Path],
    tmp_path: Path,
) -> None:
    """Full ingest writes one parquet partition per trading date
    under data/raw/source=mes_options_chain/date=YYYY-MM-DD/.
    """
    source = load_data_source("mes_options_chain")
    result = ingest_databento_options_pair(
        mes_options_x1a_csv_pair["definition"],
        mes_options_x1a_csv_pair["ohlcv_1m"],
        source,
        out_dir=tmp_path,
    )
    assert result.rows_out > 0
    assert len(result.partitions_written) > 0
    # Standard layout: source root + date=*/data.parquet
    for part in result.partitions_written:
        assert part.parent.name.startswith("date="), part.parent
        assert part.name == "data.parquet"


def test_ingest_receipt_records_combo_count(
    mes_options_x1a_csv_pair: dict[str, Path],
    tmp_path: Path,
) -> None:
    """The receipt must include combo_bars_filtered for audit."""
    source = load_data_source("mes_options_chain")
    result = ingest_databento_options_pair(
        mes_options_x1a_csv_pair["definition"],
        mes_options_x1a_csv_pair["ohlcv_1m"],
        source,
        out_dir=tmp_path,
    )
    receipt_dir = result.raw_path / "_receipts"
    receipts = list(receipt_dir.glob("*.json"))
    assert len(receipts) == 1
    import json
    payload = json.loads(receipts[0].read_text())
    assert "combo_bars_filtered" in payload
    assert payload["combo_bars_filtered"] >= 0
    assert payload["format"] == "databento_options_csv"


def test_ingest_partitions_have_canonical_schema(
    mes_options_x1a_csv_pair: dict[str, Path],
    tmp_path: Path,
) -> None:
    """Each parquet partition must carry the canonical column set."""
    source = load_data_source("mes_options_chain")
    result = ingest_databento_options_pair(
        mes_options_x1a_csv_pair["definition"],
        mes_options_x1a_csv_pair["ohlcv_1m"],
        source,
        out_dir=tmp_path,
    )
    expected_cols = {
        "ts_utc", "instrument_id", "symbol",
        "open", "high", "low", "close", "volume",
        "raw_symbol", "underlying", "expiry", "strike", "side",
    }
    sample = pl.read_parquet(result.partitions_written[0])
    assert set(sample.columns) == expected_cols
