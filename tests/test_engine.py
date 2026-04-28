from __future__ import annotations

import polars as pl

from tradegy.audit.basic import audit_source
from tradegy.features.engine import compute_feature, read_feature
from tradegy.ingest.csv_es import ingest_csv
from tradegy.registry.loader import load_data_source


def test_compute_feature_chain_through_realized_vol(synthetic_csv, workspace):
    source = load_data_source("es_ticks")
    ingest_csv(synthetic_csv, source, input_tz="UTC", out_dir=workspace["raw"])

    bars = compute_feature(
        "es_1m_bars", raw_root=workspace["raw"], feature_root=workspace["features"]
    )
    assert bars.rows >= 30

    rets = compute_feature(
        "es_1m_log_returns",
        raw_root=workspace["raw"],
        feature_root=workspace["features"],
    )
    assert rets.rows == bars.rows - 1

    rv = compute_feature(
        "realized_vol_5m",
        raw_root=workspace["raw"],
        feature_root=workspace["features"],
    )
    assert rv.rows == rets.rows - 4

    df = read_feature(
        "realized_vol_5m", root=workspace["features"]
    )
    assert (df.get_column("value") >= 0).all()


def test_audit_clean_synthetic_data_no_critical(synthetic_csv, workspace):
    source = load_data_source("es_ticks")
    ingest_csv(synthetic_csv, source, input_tz="UTC", out_dir=workspace["raw"])
    report = audit_source(
        source, raw_root=workspace["raw"], out_dir=workspace["audits"]
    )
    assert not report.has_critical
