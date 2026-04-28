"""Stage 4 — Feature Computation engine.

Loads a Feature spec, resolves its inputs (registered data sources or other
features), dispatches to a registered transform, and writes the resulting
series to data/features/feature=<id>/version=<v>/date=YYYY-MM-DD/data.parquet.

The engine never applies availability_latency to stored values — values are
stored at their *computation* timestamp. The retrieval API
(tradegy.registry.api) applies the latency offset at query time. This keeps
historical backfills idempotent under latency policy changes.

Compose-by-stages: a Feature spec's `computation` is a single registered
transform. To build composed features (e.g. realized vol from raw ticks)
we declare the chain in `inputs` as the ID of an upstream feature, and the
engine recurses to materialize the upstream feature first.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import polars as pl

from tradegy import config
from tradegy.features.transforms import get_transform
from tradegy.ingest.csv_es import read_raw
from tradegy.registry.loader import load_data_source, load_feature
from tradegy.types import Feature


@dataclass
class ComputeResult:
    feature_id: str
    feature_version: str
    rows: int
    coverage_start: datetime
    coverage_end: datetime
    out_path: Path
    inputs_resolved: dict[str, str]


def _feature_dir(
    feature: Feature, *, root: Path | None = None, version: str | None = None
) -> Path:
    base = root or config.features_dir()
    return base / f"feature={feature.id}" / f"version={version or feature.version}"


def read_feature(
    feature_id: str,
    *,
    version: str | None = None,
    root: Path | None = None,
    registry_root: Path | None = None,
) -> pl.DataFrame:
    feature = load_feature(feature_id, registry_root=registry_root)
    base = _feature_dir(feature, root=root, version=version)
    if not base.exists():
        raise FileNotFoundError(f"feature not materialized: {base}")
    pattern = str(base / "date=*" / "data.parquet")
    return pl.read_parquet(pattern).sort("ts_utc")


def _resolve_inputs(
    feature: Feature,
    *,
    raw_root: Path | None,
    feature_root: Path | None,
    registry_root: Path | None,
) -> tuple[dict[str, pl.DataFrame], dict[str, str]]:
    """Resolve a Feature's inputs into named DataFrames for the transform.

    Convention: each Feature.inputs entry must specify exactly one of
    (source_id, feature_id). The transform receives a dict whose keys are
    the input slot names declared by the transform's contract; we infer
    the slot name from the input order using the transform's documented
    convention (resample_ohlcv -> "ticks", log_return -> "bars",
    rolling_realized_vol -> "returns"). Each Feature spec must declare
    its inputs in that contract order.
    """
    slot_name_for_transform = {
        "resample_ohlcv": ["ticks"],
        "resample_ohlcv_bars": ["bars"],
        "log_return": ["bars"],
        "rolling_realized_vol": ["returns"],
        "true_range": ["bars"],
        "rolling_mean": ["series"],
        "rolling_zscore": ["series"],
        "session_position": ["bars"],
    }
    transform_id = feature.computation.transform_id
    if transform_id not in slot_name_for_transform:
        raise KeyError(f"unknown input contract for transform '{transform_id}'")
    slots = slot_name_for_transform[transform_id]
    if len(feature.inputs) != len(slots):
        raise ValueError(
            f"feature '{feature.id}' has {len(feature.inputs)} inputs but "
            f"transform '{transform_id}' expects {len(slots)}"
        )

    frames: dict[str, pl.DataFrame] = {}
    resolved: dict[str, str] = {}
    for slot, inp in zip(slots, feature.inputs, strict=True):
        if inp.source_id and inp.feature_id:
            raise ValueError(
                f"input must specify exactly one of source_id|feature_id"
            )
        if inp.source_id:
            load_data_source(inp.source_id, registry_root=registry_root)  # validate exists
            frames[slot] = read_raw(inp.source_id, root=raw_root)
            resolved[slot] = f"source:{inp.source_id}"
        elif inp.feature_id:
            upstream = load_feature(inp.feature_id, registry_root=registry_root)
            frames[slot] = read_feature(
                upstream.id,
                version=upstream.version,
                root=feature_root,
                registry_root=registry_root,
            )
            resolved[slot] = f"feature:{upstream.id}@{upstream.version}"
        else:
            raise ValueError("input must specify source_id or feature_id")
    return frames, resolved


def compute_feature(
    feature_id: str,
    *,
    raw_root: Path | None = None,
    feature_root: Path | None = None,
    registry_root: Path | None = None,
    truncate_at: datetime | None = None,
) -> ComputeResult:
    """Compute a registered feature end-to-end.

    Args:
        truncate_at: if provided, all input frames are truncated to rows with
            ts_utc <= truncate_at before dispatch. Used by the no-lookahead
            audit to reconstruct values from pre-T history.
    """
    feature = load_feature(feature_id, registry_root=registry_root)
    inputs, resolved = _resolve_inputs(
        feature,
        raw_root=raw_root,
        feature_root=feature_root,
        registry_root=registry_root,
    )

    if truncate_at is not None:
        cutoff = truncate_at
        inputs = {k: v.filter(pl.col("ts_utc") <= cutoff) for k, v in inputs.items()}

    fn = get_transform(feature.computation.transform_id)
    out = fn(inputs, dict(feature.computation.parameters))

    if "ts_utc" not in out.columns:
        raise ValueError(
            f"transform {feature.computation.transform_id} must produce a ts_utc column"
        )
    out = out.sort("ts_utc")

    if truncate_at is None:
        return _persist(feature, out, resolved, feature_root=feature_root)

    return ComputeResult(
        feature_id=feature.id,
        feature_version=feature.version,
        rows=out.height,
        coverage_start=out.select(pl.col("ts_utc").min()).item() if out.height else _epoch(),
        coverage_end=out.select(pl.col("ts_utc").max()).item() if out.height else _epoch(),
        out_path=Path("/dev/null"),
        inputs_resolved=resolved,
    )


def compute_feature_in_memory(
    feature_id: str,
    *,
    raw_root: Path | None = None,
    feature_root: Path | None = None,
    registry_root: Path | None = None,
    truncate_at: datetime | None = None,
) -> pl.DataFrame:
    """Recompute a feature without persisting; used by validators."""
    feature = load_feature(feature_id, registry_root=registry_root)
    inputs, _ = _resolve_inputs(
        feature,
        raw_root=raw_root,
        feature_root=feature_root,
        registry_root=registry_root,
    )
    if truncate_at is not None:
        inputs = {
            k: v.filter(pl.col("ts_utc") <= truncate_at) for k, v in inputs.items()
        }
    fn = get_transform(feature.computation.transform_id)
    out = fn(inputs, dict(feature.computation.parameters))
    if "ts_utc" not in out.columns:
        raise ValueError(
            f"transform {feature.computation.transform_id} must produce a ts_utc column"
        )
    return out.sort("ts_utc")


def _persist(
    feature: Feature,
    out: pl.DataFrame,
    inputs_resolved: dict[str, str],
    *,
    feature_root: Path | None,
) -> ComputeResult:
    base = _feature_dir(feature, root=feature_root)
    if base.exists():
        for existing in base.rglob("*.parquet"):
            existing.unlink()
    base.mkdir(parents=True, exist_ok=True)

    if out.height == 0:
        meta_path = base / "_meta.json"
        meta_path.write_text(json.dumps({"rows": 0}))
        return ComputeResult(
            feature_id=feature.id,
            feature_version=feature.version,
            rows=0,
            coverage_start=_epoch(),
            coverage_end=_epoch(),
            out_path=base,
            inputs_resolved=inputs_resolved,
        )

    grouped = out.with_columns(pl.col("ts_utc").dt.date().alias("_date"))
    for date_val, partition_df in grouped.group_by("_date", maintain_order=True):
        d = date_val[0]
        part_dir = base / f"date={d.isoformat()}"
        part_dir.mkdir(parents=True, exist_ok=True)
        partition_df.drop("_date").sort("ts_utc").write_parquet(
            part_dir / "data.parquet", compression="zstd"
        )

    coverage_start: datetime = out.select(pl.col("ts_utc").min()).item()
    coverage_end: datetime = out.select(pl.col("ts_utc").max()).item()
    meta = {
        "feature_id": feature.id,
        "feature_version": feature.version,
        "transform_id": feature.computation.transform_id,
        "parameters": feature.computation.parameters,
        "inputs_resolved": inputs_resolved,
        "rows": out.height,
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "computed_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    (base / "_meta.json").write_text(json.dumps(meta, indent=2))

    return ComputeResult(
        feature_id=feature.id,
        feature_version=feature.version,
        rows=out.height,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        out_path=base,
        inputs_resolved=inputs_resolved,
    )


def _epoch() -> datetime:
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


__all__ = [
    "compute_feature",
    "compute_feature_in_memory",
    "read_feature",
    "ComputeResult",
]
