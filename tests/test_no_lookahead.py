from __future__ import annotations

from datetime import timedelta

import polars as pl

from tradegy.features.engine import compute_feature
from tradegy.ingest.csv_es import ingest_csv
from tradegy.registry.api import get_feature, value_at
from tradegy.registry.loader import load_data_source
from tradegy.validate.no_lookahead import audit_no_lookahead
from tradegy.validate.reproducibility import check_reproducibility


def _setup(synthetic_csv, workspace):
    source = load_data_source("es_ticks")
    ingest_csv(synthetic_csv, source, input_tz="UTC", out_dir=workspace["raw"])
    for fid in ("es_1m_bars", "es_1m_log_returns", "realized_vol_5m"):
        compute_feature(
            fid, raw_root=workspace["raw"], feature_root=workspace["features"]
        )


def test_no_lookahead_passes_for_realized_vol(synthetic_csv, workspace):
    _setup(synthetic_csv, workspace)
    res = audit_no_lookahead(
        "realized_vol_5m",
        samples=20,
        seed=1,
        raw_root=workspace["raw"],
        feature_root=workspace["features"],
    )
    assert res.passed, res.mismatches


def test_reproducibility_passes(synthetic_csv, workspace):
    _setup(synthetic_csv, workspace)
    res = check_reproducibility(
        "realized_vol_5m",
        raw_root=workspace["raw"],
        feature_root=workspace["features"],
    )
    assert res.passed


def test_retrieval_applies_availability_latency(synthetic_csv, workspace):
    _setup(synthetic_csv, workspace)
    df = get_feature(
        "realized_vol_5m",
        feature_root=workspace["features"],
    )
    served = df.get_column("served_at")
    ts = df.get_column("ts_utc")
    diffs = (served - ts).dt.total_seconds().to_list()
    assert all(d == 1 for d in diffs)


def test_as_of_filter_excludes_future_values(synthetic_csv, workspace):
    _setup(synthetic_csv, workspace)
    full = get_feature("realized_vol_5m", feature_root=workspace["features"])
    midpoint = full.get_column("ts_utc").to_list()[len(full) // 2]
    cut = get_feature(
        "realized_vol_5m", as_of=midpoint, feature_root=workspace["features"]
    )
    assert cut.height < full.height
    assert (cut.get_column("served_at") <= midpoint).all()


def test_value_at_returns_latest_known(synthetic_csv, workspace):
    _setup(synthetic_csv, workspace)
    full = get_feature("realized_vol_5m", feature_root=workspace["features"])
    latest_ts = full.get_column("ts_utc").to_list()[-1]
    served_at = latest_ts + timedelta(seconds=1)
    res = value_at("realized_vol_5m", served_at, feature_root=workspace["features"])
    assert res is not None
    assert res["ts_utc"] == latest_ts
