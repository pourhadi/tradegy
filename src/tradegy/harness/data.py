"""Bar + feature stream loader for the backtest harness.

At each step the runner consumes a (Bar, FeatureSnapshot) pair. This
module pre-joins all required feature streams onto the bar timeline
using `served_at <= bar.ts_utc` semantics so the harness honors each
feature's declared availability_latency without per-bar registry calls.

`served_at = ts_utc + availability_latency_seconds` is computed by the
registry API at retrieval time (see src/tradegy/registry/api.py); we
fold those columns into a single asof-joined frame so iteration is just
a row scan.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import polars as pl

from tradegy.features.engine import read_feature
from tradegy.registry.api import get_feature
from tradegy.registry.loader import load_feature
from tradegy.strategies.types import Bar, FeatureSnapshot


def _bar_feature_id(instrument: str, bar_cadence: str = "1m") -> str:
    """Convention: ``{instrument_lowercase}_{cadence}_bars``.

    A future spec field could override this; for the MVP we derive from
    instrument so specs stay terse.
    """
    return f"{instrument.lower()}_{bar_cadence}_bars"


def load_bar_stream(
    instrument: str,
    *,
    bar_cadence: str = "1m",
    start: datetime | None = None,
    end: datetime | None = None,
    feature_root: Path | None = None,
) -> pl.DataFrame:
    """Read the canonical bar feature for ``instrument`` over [start, end].

    Returns the raw bars frame (ts_utc, open, high, low, close, volume,
    plus any flow columns). Caller filters columns as needed.
    """
    fid = _bar_feature_id(instrument, bar_cadence)
    df = read_feature(fid, root=feature_root).sort("ts_utc")
    if start is not None:
        df = df.filter(pl.col("ts_utc") >= start)
    if end is not None:
        df = df.filter(pl.col("ts_utc") <= end)
    return df


def build_feature_panel(
    bars: pl.DataFrame,
    feature_ids: list[str],
    *,
    feature_root: Path | None = None,
) -> pl.DataFrame:
    """Join each required feature onto the bar timeline by served_at.

    Returns the bars frame with one extra column per feature_id holding
    the latest value where served_at <= bar.ts_utc. Bars before any
    given feature has its first served_at get null for that feature.
    """
    out = bars.sort("ts_utc")
    for fid in feature_ids:
        feat = get_feature(fid, feature_root=feature_root).sort("served_at")
        if "served_at" not in feat.columns:
            raise ValueError(
                f"feature {fid} did not return a served_at column from get_feature"
            )
        # `served_at` is built via `pl.duration(seconds=...)` which yields
        # μs precision; bars.ts_utc is ns. Normalize so join_asof's
        # type-equality check is happy.
        feat = feat.with_columns(
            pl.col("served_at").cast(pl.Datetime("ns", "UTC")),
            pl.col("ts_utc").cast(pl.Datetime("ns", "UTC")),
        )
        # Identify the value column(s). get_feature returns ts_utc + value
        # columns + served_at; for our MVP we expect exactly one numeric
        # value column called "value" (engine convention), but bars-shaped
        # features have open/high/low/close/etc. Take everything that
        # isn't ts_utc/served_at and prefix with the feature id.
        rename_map = {
            c: f"{fid}__{c}" if c not in ("ts_utc", "served_at") else c
            for c in feat.columns
        }
        feat = feat.rename(rename_map).drop("ts_utc")
        # join_asof on ts_utc (left) → served_at (right), backward strategy.
        out = out.join_asof(
            feat.sort("served_at"),
            left_on="ts_utc",
            right_on="served_at",
            strategy="backward",
        )
    return out


def iter_bars_with_features(
    panel: pl.DataFrame, feature_ids: list[str]
) -> Iterator[tuple[Bar, FeatureSnapshot]]:
    """Yield (Bar, FeatureSnapshot) pairs from the joined panel."""
    for row in panel.iter_rows(named=True):
        bar = Bar(
            ts_utc=row["ts_utc"],
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume", 0.0) or 0.0),
        )
        values: dict[str, float] = {}
        for fid in feature_ids:
            # Single-value features land as `<fid>__value`; bars-shaped
            # features land as multiple `<fid>__<col>` keys. For the MVP
            # we expose the canonical "value" column when present, and
            # otherwise expose `<fid>` mapped to the close column.
            value_key = f"{fid}__value"
            if value_key in row and row[value_key] is not None:
                values[fid] = float(row[value_key])
                continue
            close_key = f"{fid}__close"
            if close_key in row and row[close_key] is not None:
                values[fid] = float(row[close_key])
        yield bar, FeatureSnapshot(ts_utc=row["ts_utc"], values=values)


def required_feature_ids_for_strategy(strategy_class_id: str) -> list[str]:
    """Return the list of feature ids the strategy class declares as
    required. Used by the runner to build the feature panel.
    """
    from tradegy.strategies.base import get_strategy_class

    cls = get_strategy_class(strategy_class_id)
    return list(cls.feature_dependencies.get("required", []))
