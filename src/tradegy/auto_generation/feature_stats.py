"""Feature-distribution stats for the auto-generator's prompt context.

The dry run on 2026-05-01 surfaced a real failure mode: the LLM picked
threshold values without knowing each feature's actual distribution
(e.g. `mes_realized_vol_30m < 0.0009` when typical values are
0.05–0.30 annualized), so its variants didn't fire any trades. This
module computes per-feature distribution stats — count, min, max,
p10, median, p90 — and exposes them as a cacheable JSON record.

The variant generator's prompt embeds these stats next to each
feature id so the LLM proposes thresholds inside the live
distribution. Same idea as a doc-string for a parameter: tell the
caller what shape of value is appropriate.

Storage: `data/feature_stats/<feature_id>.json`. Keyed by feature id
+ schema-version (for future field changes); the cache is regenerated
on demand via `compute_feature_stats(refresh=True)` or pre-warmed via
the `tradegy refresh-feature-stats` CLI.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from tradegy import config
from tradegy.features.engine import read_feature


_log = logging.getLogger(__name__)

STATS_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class FeatureStats:
    """Per-feature distribution snapshot.

    All quantile fields are None when the feature isn't materialized
    (no parquet on disk) or has zero rows. `note` carries the human-
    readable reason in that case.
    """

    feature_id: str
    rows: int
    p10: float | None
    p25: float | None
    median: float | None
    p75: float | None
    p90: float | None
    min: float | None
    max: float | None
    computed_at: str  # ISO 8601 UTC
    schema_version: str = STATS_SCHEMA_VERSION
    note: str = ""

    @property
    def is_available(self) -> bool:
        return self.rows > 0 and self.median is not None


# ─── On-disk cache ───────────────────────────────────────────────


def _stats_dir(*, root: Path | None = None) -> Path:
    return (root or config.data_dir()) / "feature_stats"


def _stats_path(feature_id: str, *, root: Path | None = None) -> Path:
    return _stats_dir(root=root) / f"{feature_id}.json"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def write_stats(stats: FeatureStats, *, root: Path | None = None) -> Path:
    p = _stats_path(stats.feature_id, root=root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(stats), indent=2))
    return p


def read_stats(
    feature_id: str, *, root: Path | None = None
) -> FeatureStats | None:
    p = _stats_path(feature_id, root=root)
    if not p.exists():
        return None
    raw = json.loads(p.read_text())
    if raw.get("schema_version") != STATS_SCHEMA_VERSION:
        return None  # cache miss on schema change; caller recomputes
    return FeatureStats(**raw)


# ─── Computation ─────────────────────────────────────────────────


def _compute_from_dataframe(
    feature_id: str, df: pl.DataFrame
) -> FeatureStats:
    """Pick the canonical numeric column from a feature parquet and
    compute quantile stats.

    Convention (from `harness/data.py:104-130`): single-value features
    expose `value`; bar-shaped features expose OHLCV columns. We pick
    `value` if present, else `close`, else the first non-`ts_utc`
    numeric column. Anything else returns "no_value_column" with no
    stats.
    """
    cols = [c for c in df.columns if c != "ts_utc"]
    chosen: str | None = None
    if "value" in cols:
        chosen = "value"
    elif "close" in cols:
        chosen = "close"
    else:
        for c in cols:
            dtype = df.schema[c]
            if dtype.is_numeric():
                chosen = c
                break
    if chosen is None:
        return FeatureStats(
            feature_id=feature_id, rows=df.height,
            p10=None, p25=None, median=None, p75=None, p90=None,
            min=None, max=None,
            computed_at=_now_iso(),
            note=f"no numeric column; available: {cols}",
        )

    s = df.get_column(chosen).drop_nulls()
    n = s.len()
    if n == 0:
        return FeatureStats(
            feature_id=feature_id, rows=0,
            p10=None, p25=None, median=None, p75=None, p90=None,
            min=None, max=None,
            computed_at=_now_iso(),
            note="zero non-null rows",
        )

    return FeatureStats(
        feature_id=feature_id,
        rows=int(n),
        p10=float(s.quantile(0.10) or 0.0),
        p25=float(s.quantile(0.25) or 0.0),
        median=float(s.quantile(0.50) or 0.0),
        p75=float(s.quantile(0.75) or 0.0),
        p90=float(s.quantile(0.90) or 0.0),
        min=float(s.min() or 0.0),
        max=float(s.max() or 0.0),
        computed_at=_now_iso(),
        note=f"column={chosen}",
    )


def compute_feature_stats(
    feature_id: str,
    *,
    feature_root: Path | None = None,
    cache_root: Path | None = None,
    refresh: bool = False,
) -> FeatureStats:
    """Return distribution stats for `feature_id`.

    Reads cached stats if present and `refresh=False`. Otherwise
    materialises stats from the on-disk feature parquet via
    `read_feature` and writes them to disk for next time.

    Features that haven't been materialised (no parquet) get a
    `FeatureStats(rows=0, note="not_materialised")` record — the
    caller renders the registry entry without a distribution but
    keeps the feature in the prompt.
    """
    if not refresh:
        cached = read_stats(feature_id, root=cache_root)
        if cached is not None:
            return cached

    try:
        df = read_feature(feature_id, root=feature_root)
    except FileNotFoundError:
        stats = FeatureStats(
            feature_id=feature_id, rows=0,
            p10=None, p25=None, median=None, p75=None, p90=None,
            min=None, max=None,
            computed_at=_now_iso(),
            note="not_materialised",
        )
    except Exception as exc:  # noqa: BLE001
        stats = FeatureStats(
            feature_id=feature_id, rows=0,
            p10=None, p25=None, median=None, p75=None, p90=None,
            min=None, max=None,
            computed_at=_now_iso(),
            note=f"read_feature_raised:{type(exc).__name__}",
        )
        _log.warning("feature_stats: read_feature(%s) failed: %r", feature_id, exc)
    else:
        stats = _compute_from_dataframe(feature_id, df)

    write_stats(stats, root=cache_root)
    return stats


def compute_all_feature_stats(
    feature_ids: list[str] | tuple[str, ...],
    *,
    feature_root: Path | None = None,
    cache_root: Path | None = None,
    refresh: bool = False,
) -> dict[str, FeatureStats]:
    """Compute stats for every feature in `feature_ids`."""
    out: dict[str, FeatureStats] = {}
    for fid in feature_ids:
        out[fid] = compute_feature_stats(
            fid,
            feature_root=feature_root,
            cache_root=cache_root,
            refresh=refresh,
        )
    return out


# ─── Prompt rendering ───────────────────────────────────────────


def format_feature_stats(stats: FeatureStats) -> str:
    """One-line summary for the cached registry block."""
    if not stats.is_available:
        note = f"  ({stats.note})" if stats.note else ""
        return f"  - {stats.feature_id}: not yet materialised{note}"
    # Prefer significant digits over fixed precision so e.g. atr_14m
    # (~2.0) and realized_vol_30m (~0.10) both render readably.
    fmt = lambda v: f"{v:.4g}"
    return (
        f"  - {stats.feature_id}: rows={stats.rows:,}  "
        f"range=[{fmt(stats.min)}, {fmt(stats.max)}]  "
        f"p10={fmt(stats.p10)}  median={fmt(stats.median)}  "
        f"p90={fmt(stats.p90)}"
    )
