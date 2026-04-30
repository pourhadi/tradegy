"""End-to-end: ingest → audit → compute → validate → query.

Mirrors the operator workflow described in 02_feature_pipeline.md:461-484
("Accept a CSV" intake) for the slice we've implemented.
"""
from __future__ import annotations

from datetime import timedelta

from tradegy.audit.basic import audit_source
from tradegy.features.engine import compute_feature
from tradegy.ingest.csv_es import ingest_csv
from tradegy.registry.api import get_feature
from tradegy.registry.loader import load_data_source
from tradegy.validate.no_lookahead import audit_no_lookahead
from tradegy.validate.reproducibility import check_reproducibility


def test_full_pipeline(synthetic_csv, workspace):
    source = load_data_source("synth_ticks")

    ingest_csv(synthetic_csv, source, input_tz="UTC", out_dir=workspace["raw"])

    audit = audit_source(
        source, raw_root=workspace["raw"], out_dir=workspace["audits"]
    )
    assert not audit.has_critical

    for fid in ("synth_1m_bars", "synth_1m_log_returns", "synth_realized_vol_5m"):
        compute_feature(
            fid, raw_root=workspace["raw"], feature_root=workspace["features"]
        )

    nl = audit_no_lookahead(
        "synth_realized_vol_5m",
        samples=10,
        seed=7,
        raw_root=workspace["raw"],
        feature_root=workspace["features"],
    )
    assert nl.passed

    rep = check_reproducibility(
        "synth_realized_vol_5m",
        raw_root=workspace["raw"],
        feature_root=workspace["features"],
    )
    assert rep.passed

    df = get_feature("synth_realized_vol_5m", feature_root=workspace["features"])
    assert df.height > 0
    assert (df.get_column("value") >= 0).all()
