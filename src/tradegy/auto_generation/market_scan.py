"""Market-structure observations for the auto-generator's prompt context.

Today the hypothesis-generator prompt has no anchor to *current*
market state — it generates from training-corpus canon (gap fade, OR
breakout, momentum). Rounds 1–3 kept landing on those patterns
because that's what dominates the corpus, not because they're
particularly applicable to the market we're trading right now.

This module produces a small structured snapshot — vol regime, gap
behaviour, session-position concentration of large moves, volume
profile — comparing recent window (default 60 RTH sessions) to a
trailing baseline (default 5 years). Each observation carries an
`interpretation` string the LLM can read directly.

Storage: `data/market_scan/market_scan_<utc_iso>.json`. Each scan is
a one-shot snapshot; the prompt loader picks the most-recent file.
The `tradegy market-scan` CLI is the operator's pre-flight before
running `tradegy hypothesize` — refresh the scan, look at the output,
then call the LLM.

Implementation notes:

* Data source is the materialized `mes_1m_bars` parquet (or whatever
  bar feature the operator points at) plus `mes_xnys_session_position`
  for session-position bucketing. Both are read via the standard
  `read_feature` path so any future re-ingest flows through cleanly.
* Only RTH bars (`mes_xnys_session_position` ∈ [0, 1]) are used. The
  globex overnight is loud noise for these statistics and would
  drown out the regime signal.
* The "recent" / "baseline" split is computed from session counts,
  not calendar days, so weekends and exchange holidays don't skew
  the window.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import polars as pl

from tradegy import config


_log = logging.getLogger(__name__)

SCAN_SCHEMA_VERSION = "1"

DEFAULT_BAR_FEATURE = "mes_1m_bars"
DEFAULT_SESSION_FEATURE = "mes_xnys_session_position"
DEFAULT_RECENT_SESSIONS = 60
DEFAULT_BASELINE_SESSIONS = 5 * 252  # ~5 RTH years


@dataclass(frozen=True)
class Observation:
    """One row of the scan report.

    `current_value` and `baseline_value` are typically scalars; the
    `percentile` is current's percentile rank within the baseline
    distribution (so 0.95 means recent regime is in the top 5% of
    the baseline). `interpretation` is the human/LLM-readable summary
    line — load-bearing for the prompt context, since the LLM reads
    it directly.
    """

    metric: str
    current_value: float
    baseline_value: float
    percentile: float | None
    interpretation: str
    note: str = ""


@dataclass(frozen=True)
class MarketScanReport:
    instrument: str
    bar_feature: str
    session_feature: str
    recent_sessions: int
    baseline_sessions: int
    recent_window: tuple[str, str]   # ISO timestamps of first/last bar
    baseline_window: tuple[str, str]
    observations: tuple[Observation, ...]
    computed_at: str  # ISO 8601 UTC
    schema_version: str = SCAN_SCHEMA_VERSION


# ── Computation primitives ───────────────────────────────────────


def _percentile_rank(value: float, distribution: list[float]) -> float | None:
    """Fraction of `distribution` strictly less than `value`. None
    when distribution is empty or value is non-finite.
    """
    if not distribution or not math.isfinite(value):
        return None
    less = sum(1 for x in distribution if math.isfinite(x) and x < value)
    return less / len(distribution)


def _split_recent_baseline(
    bars: pl.DataFrame,
    *,
    session_pos_col: str,
    recent_sessions: int,
    baseline_sessions: int,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Slice `bars` (sorted by ts_utc) into recent and baseline
    windows by RTH session count. Sessions are identified by
    `session_position == 0.0` boundary crossings (the first bar of
    each RTH session resets to 0).

    Returns (recent_df, baseline_df). Either may be empty if the data
    is too short — the caller decides what to emit.
    """
    if "session_id" in bars.columns:
        boundary_ids = bars["session_id"].unique().to_list()
    else:
        # Fall back: derive a session id from the session-position
        # reset pattern. A new RTH session starts when session_pos
        # transitions from > 0 (or null) to 0.0.
        with_id = bars.with_columns(
            pl.col(session_pos_col).fill_null(-1.0).alias("_sp"),
        ).with_columns(
            (pl.col("_sp") == 0.0).cum_sum().alias("session_id"),
        )
        boundary_ids = with_id["session_id"].unique().to_list()
        bars = with_id.drop("_sp")

    boundary_ids.sort()
    if not boundary_ids:
        return bars[:0], bars[:0]

    # Drop session_id == 0 entries (pre-first-RTH-session bars).
    boundary_ids = [s for s in boundary_ids if s > 0]
    if not boundary_ids:
        return bars[:0], bars[:0]

    n = len(boundary_ids)
    recent_n = min(recent_sessions, n)
    baseline_n = min(baseline_sessions, max(0, n - recent_n))

    recent_ids = set(boundary_ids[-recent_n:])
    baseline_ids = set(
        boundary_ids[-(recent_n + baseline_n) : -recent_n]
    ) if baseline_n > 0 else set()

    recent_df = bars.filter(pl.col("session_id").is_in(list(recent_ids)))
    baseline_df = bars.filter(pl.col("session_id").is_in(list(baseline_ids)))
    return recent_df, baseline_df


def _window_endpoints(df: pl.DataFrame) -> tuple[str, str]:
    if df.is_empty():
        return ("", "")
    first = df["ts_utc"].min()
    last = df["ts_utc"].max()
    return (str(first), str(last))


def _compute_realized_vol_session(
    bars: pl.DataFrame,
    *,
    close_col: str = "close",
) -> list[float]:
    """One realized-vol observation per session: stdev of 1m log
    returns. Returned as a plain Python list to feed `_percentile_rank`.
    """
    if bars.is_empty() or "session_id" not in bars.columns:
        return []
    df = bars.with_columns(
        (pl.col(close_col) / pl.col(close_col).shift(1)).log().alias("_logret"),
    )
    by_session = (
        df.group_by("session_id")
        .agg(pl.col("_logret").std().alias("rv"))
        .drop_nulls("rv")
    )
    return by_session["rv"].to_list()


def _compute_overnight_gaps(
    bars: pl.DataFrame,
    *,
    open_col: str = "open",
    close_col: str = "close",
    session_pos_col: str,
) -> list[float]:
    """One signed-pct gap observation per session: open_first / close_last_prev - 1.
    Skips sessions with no prior session (first session).
    """
    if bars.is_empty() or "session_id" not in bars.columns:
        return []
    by_session = (
        bars.sort("ts_utc")
        .group_by("session_id", maintain_order=True)
        .agg([
            pl.col(open_col).first().alias("session_open"),
            pl.col(close_col).last().alias("session_close"),
        ])
    )
    by_session = by_session.with_columns(
        (
            pl.col("session_open")
            / pl.col("session_close").shift(1)
            - 1.0
        ).alias("gap_pct"),
    ).drop_nulls("gap_pct")
    return by_session["gap_pct"].to_list()


def _compute_top_decile_session_position(
    bars: pl.DataFrame,
    *,
    close_col: str = "close",
    session_pos_col: str,
) -> list[float]:
    """For each session, find the bar with the largest |1m log return|;
    return that bar's session_position. The point is: are the day's
    biggest moves clustering in early-RTH (open drive), midday, or
    afternoon (close ramps) right now?
    """
    if bars.is_empty() or "session_id" not in bars.columns:
        return []
    df = bars.with_columns(
        (pl.col(close_col) / pl.col(close_col).shift(1)).log().abs().alias("_aret"),
    ).drop_nulls("_aret")
    if df.is_empty():
        return []
    biggest = (
        df.sort("_aret", descending=True)
        .group_by("session_id", maintain_order=True)
        .agg(pl.col(session_pos_col).first().alias("pos"))
        .drop_nulls("pos")
    )
    return biggest["pos"].to_list()


def _compute_session_volume(
    bars: pl.DataFrame, *, volume_col: str = "volume",
) -> list[float]:
    if bars.is_empty() or "session_id" not in bars.columns or volume_col not in bars.columns:
        return []
    by_session = (
        bars.group_by("session_id")
        .agg(pl.col(volume_col).sum().alias("v"))
        .drop_nulls("v")
    )
    return [float(x) for x in by_session["v"].to_list()]


def _scalar_summary(values: list[float]) -> tuple[float, float]:
    """(median, stdev) of a value list. Returns (nan, nan) if empty."""
    if not values:
        return (float("nan"), float("nan"))
    s = pl.Series("v", values)
    median = s.median()
    stdev = s.std()
    return (
        float(median) if median is not None else float("nan"),
        float(stdev) if stdev is not None else float("nan"),
    )


def _interpret_regime(
    metric: str,
    pct: float | None,
    *,
    high_label: str,
    low_label: str,
) -> str:
    if pct is None:
        return f"{metric}: insufficient data for percentile rank"
    if pct >= 0.85:
        band = high_label
    elif pct <= 0.15:
        band = low_label
    else:
        band = "near baseline median"
    return f"{metric}: recent at p{pct * 100:.0f} of baseline — {band}"


# ── Top-level scan ───────────────────────────────────────────────


def compute_market_scan(
    *,
    bar_feature: str = DEFAULT_BAR_FEATURE,
    session_feature: str = DEFAULT_SESSION_FEATURE,
    recent_sessions: int = DEFAULT_RECENT_SESSIONS,
    baseline_sessions: int = DEFAULT_BASELINE_SESSIONS,
    instrument: str = "MES",
    feature_root: Path | None = None,
    registry_root: Path | None = None,
) -> MarketScanReport:
    """Run the full scan. Reads `bar_feature` (OHLCV) and
    `session_feature` (RTH session-position 0..1) from disk, slices
    into recent/baseline by session count, and emits an Observation
    for each metric in the standard set.

    Raises FileNotFoundError if the bar feature isn't materialized —
    the caller surfaces that to the operator. There is intentionally
    no fallback (per project rule: no fallback logic).
    """
    from tradegy.features.engine import read_feature

    bars = read_feature(
        bar_feature, root=feature_root, registry_root=registry_root,
    )
    sess = read_feature(
        session_feature, root=feature_root, registry_root=registry_root,
    )

    sp_col = "value" if "value" in sess.columns else session_feature
    sess_renamed = sess.select(["ts_utc", pl.col(sp_col).alias(session_feature)])

    merged = bars.join(sess_renamed, on="ts_utc", how="inner").sort("ts_utc")
    rth = merged.filter(
        (pl.col(session_feature) >= 0.0) & (pl.col(session_feature) <= 1.0)
    )

    rth = rth.with_columns(
        (pl.col(session_feature) == 0.0).cum_sum().alias("session_id"),
    )

    recent, baseline = _split_recent_baseline(
        rth,
        session_pos_col=session_feature,
        recent_sessions=recent_sessions,
        baseline_sessions=baseline_sessions,
    )

    observations: list[Observation] = []

    rv_recent = _compute_realized_vol_session(recent)
    rv_baseline = _compute_realized_vol_session(baseline)
    rv_curr_med, _ = _scalar_summary(rv_recent)
    rv_base_med, _ = _scalar_summary(rv_baseline)
    rv_pct = _percentile_rank(rv_curr_med, rv_baseline)
    observations.append(Observation(
        metric="realized_vol_per_session",
        current_value=rv_curr_med,
        baseline_value=rv_base_med,
        percentile=rv_pct,
        interpretation=_interpret_regime(
            "realized vol", rv_pct,
            high_label="high-vol regime (mean-reversion / fade strategies favoured)",
            low_label="low-vol regime (breakout / drift strategies favoured)",
        ),
    ))

    gaps_recent = _compute_overnight_gaps(
        recent, session_pos_col=session_feature,
    )
    gaps_baseline = _compute_overnight_gaps(
        baseline, session_pos_col=session_feature,
    )
    gap_curr_abs = (
        sum(abs(g) for g in gaps_recent) / len(gaps_recent)
        if gaps_recent else float("nan")
    )
    gap_base_abs = (
        sum(abs(g) for g in gaps_baseline) / len(gaps_baseline)
        if gaps_baseline else float("nan")
    )
    gap_pct = _percentile_rank(
        gap_curr_abs, [abs(g) for g in gaps_baseline],
    )
    observations.append(Observation(
        metric="abs_overnight_gap",
        current_value=gap_curr_abs,
        baseline_value=gap_base_abs,
        percentile=gap_pct,
        interpretation=_interpret_regime(
            "overnight gap magnitude", gap_pct,
            high_label="large overnight moves — gap-fade and gap-continuation both have ammunition",
            low_label="quiet overnights — gap-driven strategies will see few setups",
        ),
    ))

    pos_recent = _compute_top_decile_session_position(
        recent, session_pos_col=session_feature,
    )
    pos_baseline = _compute_top_decile_session_position(
        baseline, session_pos_col=session_feature,
    )
    pos_curr_med, _ = _scalar_summary(pos_recent)
    pos_base_med, _ = _scalar_summary(pos_baseline)
    if math.isnan(pos_curr_med):
        pos_band = "n/a"
    elif pos_curr_med < 0.25:
        pos_band = "early-RTH (open-drive regime)"
    elif pos_curr_med < 0.55:
        pos_band = "midday-concentrated"
    elif pos_curr_med < 0.85:
        pos_band = "afternoon-concentrated"
    else:
        pos_band = "close-ramp regime"
    observations.append(Observation(
        metric="largest_move_session_position",
        current_value=pos_curr_med,
        baseline_value=pos_base_med,
        percentile=None,
        interpretation=(
            f"biggest 1m move per session lands at session_pos~{pos_curr_med:.2f} "
            f"recently vs ~{pos_base_med:.2f} baseline — {pos_band}"
        ),
    ))

    vol_recent = _compute_session_volume(recent)
    vol_baseline = _compute_session_volume(baseline)
    vol_curr_med, _ = _scalar_summary(vol_recent)
    vol_base_med, _ = _scalar_summary(vol_baseline)
    vol_pct = _percentile_rank(vol_curr_med, vol_baseline)
    observations.append(Observation(
        metric="session_volume",
        current_value=vol_curr_med,
        baseline_value=vol_base_med,
        percentile=vol_pct,
        interpretation=_interpret_regime(
            "session volume", vol_pct,
            high_label="active participation — liquidity-provision / mean-reversion has ammunition",
            low_label="thin participation — strategies that need depth will misfire",
        ),
    ))

    return MarketScanReport(
        instrument=instrument,
        bar_feature=bar_feature,
        session_feature=session_feature,
        recent_sessions=recent_sessions,
        baseline_sessions=baseline_sessions,
        recent_window=_window_endpoints(recent),
        baseline_window=_window_endpoints(baseline),
        observations=tuple(observations),
        computed_at=datetime.now(tz=timezone.utc).isoformat(),
    )


# ── Persistence ─────────────────────────────────────────────────


def market_scan_dir(*, root: Path | None = None) -> Path:
    base = root or config.data_dir() / "market_scan"
    return base


def write_market_scan(
    report: MarketScanReport, *, root: Path | None = None,
) -> Path:
    """Persist `report` as JSON. Filename is timestamped so old scans
    accumulate (operator can compare regime changes over time).
    """
    base = market_scan_dir(root=root)
    base.mkdir(parents=True, exist_ok=True)
    stamp = report.computed_at.replace(":", "").replace("-", "").replace(".", "")
    path = base / f"market_scan_{stamp[:15]}.json"
    payload: dict = asdict(report)
    payload["observations"] = [asdict(o) for o in report.observations]
    payload["recent_window"] = list(report.recent_window)
    payload["baseline_window"] = list(report.baseline_window)
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def read_latest_market_scan(
    *, root: Path | None = None,
) -> MarketScanReport | None:
    """Pick up the most-recent scan on disk; return None if none."""
    base = market_scan_dir(root=root)
    if not base.exists():
        return None
    candidates = sorted(base.glob("market_scan_*.json"))
    if not candidates:
        return None
    payload = json.loads(candidates[-1].read_text())
    if payload.get("schema_version") != SCAN_SCHEMA_VERSION:
        _log.info(
            "discarding stale market-scan (schema=%s, want %s)",
            payload.get("schema_version"), SCAN_SCHEMA_VERSION,
        )
        return None
    obs = tuple(Observation(**o) for o in payload.pop("observations", []))
    payload["recent_window"] = tuple(payload.get("recent_window", ("", "")))
    payload["baseline_window"] = tuple(payload.get("baseline_window", ("", "")))
    payload["observations"] = obs
    return MarketScanReport(**payload)


# ── Rendering for prompt ─────────────────────────────────────────


def format_market_scan_report(report: MarketScanReport | None) -> str:
    """Render the scan as a system-prompt block. Returns "" when the
    report is None (so the caller can skip the section).
    """
    if report is None or not report.observations:
        return ""
    lines = [
        "## Current market-structure observations",
        "",
        f"Instrument {report.instrument}; recent window "
        f"{report.recent_sessions} RTH sessions vs "
        f"{report.baseline_sessions}-session baseline. "
        "Use these to bias hypothesis ideation toward mechanisms whose "
        "edge lives in the *current* regime — proposing a low-vol "
        "compression breakout when recent vol is at p95 of baseline is "
        "wasted budget.",
        "",
    ]
    for o in report.observations:
        lines.append(f"- {o.interpretation}")
    return "\n".join(lines) + "\n"
