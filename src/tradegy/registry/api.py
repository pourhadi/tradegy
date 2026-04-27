"""Stage 7 — Feature retrieval API (pull only for v0.1).

Implements the registry queries documented in 02_feature_pipeline.md:524-529.
Q1 ("get feature X over [t0,t1] at version V, as known at as_of") and Q5
("audit trail: value at T, per version") are fully implemented; the others
are stubbed because they need the full feature registry indexed by
dependent_strategies, which arrives in a later slice.

Latency invariant: a feature value computed at ts_utc is not "known" until
ts_utc + availability_latency_seconds. When `as_of` is supplied, only rows
with served_at <= as_of are returned. The served_at column is added so
downstream consumers can verify the invariant themselves.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from tradegy.features.engine import read_feature
from tradegy.registry.loader import list_features, load_feature
from tradegy.types import Feature


def get_feature(
    feature_id: str,
    *,
    version: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    as_of: datetime | None = None,
    feature_root: Path | None = None,
    registry_root: Path | None = None,
) -> pl.DataFrame:
    """Retrieve a feature series with availability_latency pre-applied.

    Returns a DataFrame with the feature's natural columns (ts_utc + value
    columns) plus a synthesized `served_at` column = ts_utc + latency. Rows
    where `served_at > as_of` are filtered out so callers can never see a
    value before it would have been published.
    """
    feature: Feature = load_feature(feature_id, registry_root=registry_root)
    df = read_feature(
        feature.id,
        version=version or feature.version,
        root=feature_root,
        registry_root=registry_root,
    )

    latency = timedelta(seconds=int(feature.availability_latency_seconds))
    df = df.with_columns(
        (pl.col("ts_utc") + pl.duration(seconds=int(feature.availability_latency_seconds)))
        .alias("served_at")
    )

    if start is not None:
        df = df.filter(pl.col("ts_utc") >= start)
    if end is not None:
        df = df.filter(pl.col("ts_utc") <= end)
    if as_of is not None:
        df = df.filter(pl.col("served_at") <= as_of)

    return df


def value_at(
    feature_id: str,
    ts: datetime,
    *,
    version: str | None = None,
    feature_root: Path | None = None,
    registry_root: Path | None = None,
) -> dict | None:
    """Q5: audit trail — what was feature X at ts (per version)?

    Returns a dict {ts_utc, value..., served_at} or None if no row exists at
    or before ts whose served_at is <= ts.
    """
    df = get_feature(
        feature_id,
        version=version,
        end=ts,
        as_of=ts,
        feature_root=feature_root,
        registry_root=registry_root,
    )
    if df.height == 0:
        return None
    last = df.sort("ts_utc").tail(1).to_dicts()[0]
    return last


def find_features(
    *,
    cadence: str | None = None,
    max_latency_seconds: int | None = None,
    registry_root: Path | None = None,
) -> list[Feature]:
    """Q1 (filter form): list features matching cadence/latency constraints."""
    out: list[Feature] = []
    for f in list_features(registry_root=registry_root):
        if cadence is not None and f.cadence != cadence:
            continue
        if (
            max_latency_seconds is not None
            and f.availability_latency_seconds > max_latency_seconds
        ):
            continue
        out.append(f)
    return out


__all__ = ["get_feature", "value_at", "find_features"]
