"""Stage 6 — No-Lookahead audit (per 02_feature_pipeline.md:271-280).

For a sample of N timestamps T_i, recompute the feature using ONLY input
data with ts_utc <= T_i (truncated input). The recomputed value at T_i must
exactly equal the published value at T_i. If they differ, the feature
depends on data published after T_i — i.e., lookahead.

This is the "honest, expensive" check: it actually rebuilds the feature
from pre-T history. Bugs in the no-lookahead invariant cannot hide here.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import polars as pl

from tradegy.features.engine import compute_feature_in_memory, read_feature
from tradegy.registry.loader import load_feature


@dataclass
class NoLookaheadResult:
    feature_id: str
    feature_version: str
    samples: int
    matches: int
    mismatches: list[dict]

    @property
    def passed(self) -> bool:
        return self.matches == self.samples and self.samples > 0


def audit_no_lookahead(
    feature_id: str,
    *,
    samples: int = 200,
    seed: int = 0,
    raw_root: Path | None = None,
    feature_root: Path | None = None,
    registry_root: Path | None = None,
    rtol: float = 0.0,
    atol: float = 0.0,
) -> NoLookaheadResult:
    feature = load_feature(feature_id, registry_root=registry_root)
    published = read_feature(
        feature.id,
        version=feature.version,
        root=feature_root,
        registry_root=registry_root,
    )

    if published.height == 0:
        return NoLookaheadResult(
            feature_id=feature.id,
            feature_version=feature.version,
            samples=0,
            matches=0,
            mismatches=[],
        )

    rng = random.Random(seed)
    n = min(samples, published.height)
    indices = sorted(rng.sample(range(published.height), n))

    rows = published.slice(0).to_dicts()
    sampled = [rows[i] for i in indices]

    mismatches: list[dict] = []
    matches = 0
    value_columns = [c for c in published.columns if c != "ts_utc"]

    for row in sampled:
        t: datetime = row["ts_utc"]
        recomputed = compute_feature_in_memory(
            feature.id,
            raw_root=raw_root,
            feature_root=feature_root,
            registry_root=registry_root,
            truncate_at=t,
        )
        match_row = recomputed.filter(pl.col("ts_utc") == t)
        if match_row.height != 1:
            mismatches.append(
                {"ts_utc": t.isoformat(), "reason": "missing_in_recompute"}
            )
            continue
        recomputed_row = match_row.to_dicts()[0]
        ok = True
        diffs: dict[str, dict] = {}
        for col in value_columns:
            expected = row[col]
            actual = recomputed_row.get(col)
            if not _close(expected, actual, rtol=rtol, atol=atol):
                ok = False
                diffs[col] = {"expected": expected, "actual": actual}
        if ok:
            matches += 1
        else:
            mismatches.append({"ts_utc": t.isoformat(), "diffs": diffs})

    return NoLookaheadResult(
        feature_id=feature.id,
        feature_version=feature.version,
        samples=n,
        matches=matches,
        mismatches=mismatches,
    )


def _close(a, b, *, rtol: float, atol: float) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        return math.isclose(a, b, rel_tol=rtol, abs_tol=atol)
    return a == b


__all__ = ["audit_no_lookahead", "NoLookaheadResult"]
