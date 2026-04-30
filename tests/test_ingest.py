from __future__ import annotations

import polars as pl

from tradegy.ingest.csv_es import ingest_csv, read_raw
from tradegy.registry.loader import load_data_source


def test_ingest_writes_partitioned_parquet_and_normalizes_tz(synthetic_csv, workspace):
    source = load_data_source("synth_ticks")
    result = ingest_csv(
        synthetic_csv, source, input_tz="UTC", out_dir=workspace["raw"]
    )

    assert result.rows_in == result.rows_out + result.duplicates_dropped
    assert result.rows_out > 0
    assert len(result.partitions_written) == 2

    df = read_raw(source.id, root=workspace["raw"])
    assert "ts_utc" in df.columns
    assert df["ts_utc"].dtype == pl.Datetime("ns", "UTC")
    assert df.get_column("ts_utc").is_sorted()


def test_ingest_drops_exact_duplicates(synthetic_csv, workspace, tmp_path):
    import shutil

    dup_path = tmp_path / "dup.csv"
    src = synthetic_csv.read_text()
    rows = src.splitlines()
    body = rows[1:]
    dup_path.write_text("\n".join([rows[0], *body, *body[:5]]))

    source = load_data_source("synth_ticks")
    result = ingest_csv(dup_path, source, input_tz="UTC", out_dir=workspace["raw"])
    assert result.duplicates_dropped >= 5
