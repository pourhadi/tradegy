"""Reproducibility check — recompute and byte-compare against persisted output.

Per 02_feature_pipeline.md:286: "Reproducibility check: recompute random
sample from raw data; must match exactly."
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from tradegy.features.engine import compute_feature_in_memory, read_feature
from tradegy.registry.loader import load_feature


@dataclass
class ReproducibilityResult:
    feature_id: str
    feature_version: str
    rows_compared: int
    mismatches: int

    @property
    def passed(self) -> bool:
        return self.mismatches == 0 and self.rows_compared > 0


def check_reproducibility(
    feature_id: str,
    *,
    raw_root: Path | None = None,
    feature_root: Path | None = None,
    registry_root: Path | None = None,
) -> ReproducibilityResult:
    feature = load_feature(feature_id, registry_root=registry_root)
    persisted = read_feature(
        feature.id,
        version=feature.version,
        root=feature_root,
        registry_root=registry_root,
    ).sort("ts_utc")

    recomputed = compute_feature_in_memory(
        feature.id,
        raw_root=raw_root,
        feature_root=feature_root,
        registry_root=registry_root,
    ).sort("ts_utc")

    if persisted.height != recomputed.height:
        return ReproducibilityResult(
            feature_id=feature.id,
            feature_version=feature.version,
            rows_compared=min(persisted.height, recomputed.height),
            mismatches=abs(persisted.height - recomputed.height),
        )

    common_cols = [c for c in persisted.columns if c in recomputed.columns]
    p = persisted.select(common_cols)
    r = recomputed.select(common_cols)
    diff = p.equals(r)
    if diff:
        return ReproducibilityResult(
            feature_id=feature.id,
            feature_version=feature.version,
            rows_compared=persisted.height,
            mismatches=0,
        )

    n_mismatches = 0
    for col in common_cols:
        if col == "ts_utc":
            continue
        a = p.get_column(col).to_list()
        b = r.get_column(col).to_list()
        for av, bv in zip(a, b, strict=True):
            if isinstance(av, float) and isinstance(bv, float):
                if math.isnan(av) and math.isnan(bv):
                    continue
            if av != bv:
                n_mismatches += 1
    return ReproducibilityResult(
        feature_id=feature.id,
        feature_version=feature.version,
        rows_compared=persisted.height,
        mismatches=n_mismatches,
    )


__all__ = ["check_reproducibility", "ReproducibilityResult"]
