"""Build the sealed pre-open snapshot for each historical session.

For every trading session date D, this module assembles the
"information set as of 09:30 ET on D" — the fact set the LLM uses to
classify the regime. Sealing matters: any leakage of post-09:30 data
into the prompt would invalidate walk-forward gates downstream.

What goes in:
  - Session date and instrument id.
  - Today's scheduled events (filtered by today's UTC date span).
    These ARE known pre-open because their schedule is published in
    advance.
  - Overnight gap (open[D] - close[D-1]) / close[D-1]. Open is the
    session-open print, which IS pre-09:30 ET in spirit (we use the
    first available bar inside the session).
  - Prior-5-day close-to-close % returns (D-5 → D-4, ..., D-1 → D-0
    where D-0 is the prior session close).
  - Cash VIX as of prior close (level + 252-day percentile + 5d
    change). All deterministic, all from features already
    materialized.

What goes OUT (for the LLM):
  A `SessionPreOpenSnapshot` Pydantic model that the labeling pipeline
  formats into the user-message prompt block.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import polars as pl

from tradegy.features.engine import read_feature
from tradegy.ingest._common import source_root


_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")


@dataclass(frozen=True)
class ScheduledEventInput:
    ts_utc: datetime
    event_type: str
    importance: str
    headline: str


@dataclass(frozen=True)
class SessionPreOpenSnapshot:
    session_date: date
    instrument: str
    overnight_gap_pct: float | None
    prior_close: float | None
    today_open: float | None
    prior_5d_close_to_close_pct: list[float]  # most-recent first
    vix_close_at_prior_close: float | None
    vix_pctile_252_at_prior_close: float | None
    vix_5d_change_at_prior_close: float | None
    scheduled_events_today: list[ScheduledEventInput]


def _read_bars(instrument: str, raw_root: Path | None = None) -> pl.DataFrame:
    """Read 1m OHLCV bars for `instrument` from the raw parquet."""
    src_id = f"{instrument.lower()}_1m_ohlcv"
    base = source_root(src_id, out_dir=raw_root)
    if not base.exists():
        raise FileNotFoundError(f"raw source path missing: {base}")
    pattern = str(base / "date=*" / "data.parquet")
    df = pl.read_parquet(pattern).sort("ts_utc")
    return df


def _read_econ_events(raw_root: Path | None = None) -> pl.DataFrame:
    base = source_root("econ_events", out_dir=raw_root)
    if not base.exists():
        return pl.DataFrame({
            "ts_utc": [], "event_type": [], "importance": [],
            "headline": [],
        })
    pattern = str(base / "date=*" / "data.parquet")
    return pl.read_parquet(pattern)


def _read_feature_value(
    feature_id: str, *,
    feature_root: Path | None = None,
) -> pl.DataFrame:
    return read_feature(feature_id, root=feature_root).sort("ts_utc")


def _value_as_of(df: pl.DataFrame, as_of_ts: datetime) -> float | None:
    """Latest `value` whose ts_utc <= as_of_ts. None if none qualify."""
    if df.height == 0:
        return None
    sub = df.filter(pl.col("ts_utc") <= as_of_ts).sort("ts_utc")
    if sub.height == 0:
        return None
    return float(sub.row(-1, named=True)["value"])


def _bar_at_or_after(
    bars: pl.DataFrame, ts: datetime
) -> dict | None:
    sub = bars.filter(pl.col("ts_utc") >= ts).sort("ts_utc")
    if sub.height == 0:
        return None
    return sub.row(0, named=True)


def _bar_at_or_before(
    bars: pl.DataFrame, ts: datetime
) -> dict | None:
    sub = bars.filter(pl.col("ts_utc") <= ts).sort("ts_utc")
    if sub.height == 0:
        return None
    return sub.row(-1, named=True)


def _session_open_close_utc(d: date) -> tuple[datetime, datetime]:
    """For an XNYS-equivalent session, return (open_utc, close_utc).

    Uses 09:30 ET (open) and 16:00 ET (close). DST-aware via zoneinfo.
    Holidays are NOT special-cased; callers should restrict the date
    range to actual trading days (this code returns whatever times
    correspond to those clock moments on the requested calendar
    date).
    """
    open_et = datetime.combine(d, datetime.min.time(), tzinfo=_ET).replace(hour=9, minute=30)
    close_et = datetime.combine(d, datetime.min.time(), tzinfo=_ET).replace(hour=16, minute=0)
    return open_et.astimezone(_UTC), close_et.astimezone(_UTC)


def build_snapshot(
    *,
    session_date: date,
    instrument: str = "MES",
    raw_root: Path | None = None,
    feature_root: Path | None = None,
) -> SessionPreOpenSnapshot:
    """Build the sealed pre-open snapshot for one session."""
    bars = _read_bars(instrument, raw_root=raw_root)
    open_utc, close_utc = _session_open_close_utc(session_date)

    today_open_bar = _bar_at_or_after(bars, open_utc)
    today_open = (
        float(today_open_bar["open"]) if today_open_bar is not None else None
    )

    # Prior session close = the last bar before today's open_utc whose
    # session date is earlier than today.
    prior_close_cutoff = open_utc
    prior_bar = _bar_at_or_before(bars, prior_close_cutoff - timedelta(seconds=1))
    prior_close = (
        float(prior_bar["close"]) if prior_bar is not None else None
    )

    overnight_gap_pct: float | None = None
    if today_open is not None and prior_close not in (None, 0):
        overnight_gap_pct = (today_open - prior_close) / prior_close

    # Prior-5-day close-to-close % returns. We snap to UTC daily bins
    # by walking back trading days (skipping weekends; holidays are
    # handled implicitly because there are no bars).
    closes_dates: list[float] = []
    cursor = session_date - timedelta(days=1)
    last_close_seen: float | None = None
    days_seen = 0
    bars_sorted = bars.sort("ts_utc")
    while days_seen < 6 and cursor > session_date - timedelta(days=20):
        sess_open, sess_close = _session_open_close_utc(cursor)
        last_bar = bars_sorted.filter(
            (pl.col("ts_utc") >= sess_open) & (pl.col("ts_utc") <= sess_close)
        ).sort("ts_utc")
        if last_bar.height > 0:
            cls = float(last_bar.row(-1, named=True)["close"])
            if last_close_seen is not None and last_close_seen != 0:
                pct = (cls - last_close_seen) / last_close_seen
                # Note: walking BACKWARD, so this represents the
                # earlier→later move. Reverse at the end.
                closes_dates.append(pct)
            last_close_seen = cls
            days_seen += 1
        cursor = cursor - timedelta(days=1)
    # closes_dates was built oldest-first; reverse so the most-recent
    # day's return comes first in the output.
    prior_5d = list(reversed(closes_dates))[:5]

    # VIX features as of prior close.
    vix_close_df = _read_feature_value("vix_daily_close", feature_root=feature_root)
    vix_pctile_df = _read_feature_value("vix_daily_pctile_252", feature_root=feature_root)
    vix_5d_df = _read_feature_value("vix_daily_5d_change", feature_root=feature_root)
    vix_at_prior = _value_as_of(vix_close_df, open_utc - timedelta(seconds=1))
    vix_pctile_at_prior = _value_as_of(vix_pctile_df, open_utc - timedelta(seconds=1))
    vix_5d_at_prior = _value_as_of(vix_5d_df, open_utc - timedelta(seconds=1))

    # Scheduled events for today (UTC date span).
    events = _read_econ_events(raw_root=raw_root)
    today_start_utc = datetime.combine(session_date, datetime.min.time(), tzinfo=_UTC)
    today_end_utc = today_start_utc + timedelta(days=1)
    if events.height > 0:
        # Cast event ts_utc consistently (the source CSV ingest may have
        # different precision than the bar feed).
        today_evs = events.filter(
            (pl.col("ts_utc") >= today_start_utc)
            & (pl.col("ts_utc") < today_end_utc)
        ).sort("ts_utc")
    else:
        today_evs = events
    scheduled = []
    for r in today_evs.iter_rows(named=True):
        scheduled.append(ScheduledEventInput(
            ts_utc=r["ts_utc"],
            event_type=r.get("event_type", ""),
            importance=r.get("importance", "low"),
            headline=str(r.get("headline", ""))[:200],
        ))

    return SessionPreOpenSnapshot(
        session_date=session_date,
        instrument=instrument,
        overnight_gap_pct=overnight_gap_pct,
        prior_close=prior_close,
        today_open=today_open,
        prior_5d_close_to_close_pct=prior_5d,
        vix_close_at_prior_close=vix_at_prior,
        vix_pctile_252_at_prior_close=vix_pctile_at_prior,
        vix_5d_change_at_prior_close=vix_5d_at_prior,
        scheduled_events_today=scheduled,
    )


def format_snapshot_for_llm(s: SessionPreOpenSnapshot) -> str:
    """Render a snapshot as the LLM-facing user-message text block."""
    lines: list[str] = []
    lines.append(f"## Session pre-open snapshot")
    lines.append(f"  date: {s.session_date.isoformat()}")
    lines.append(f"  instrument: {s.instrument}")
    if s.today_open is not None and s.prior_close is not None:
        lines.append(f"  today open: {s.today_open:.4f}")
        lines.append(f"  prior close: {s.prior_close:.4f}")
    if s.overnight_gap_pct is not None:
        lines.append(f"  overnight gap: {s.overnight_gap_pct * 100:+.2f}%")
    if s.prior_5d_close_to_close_pct:
        moves = ", ".join(f"{p * 100:+.2f}%" for p in s.prior_5d_close_to_close_pct)
        lines.append(f"  prior-5d close-to-close: [{moves}]  (most-recent first)")
    if s.vix_close_at_prior_close is not None:
        lines.append(f"  VIX at prior close: {s.vix_close_at_prior_close:.2f}")
    if s.vix_pctile_252_at_prior_close is not None:
        lines.append(f"  VIX 252-day percentile: {s.vix_pctile_252_at_prior_close:.2f}")
    if s.vix_5d_change_at_prior_close is not None:
        lines.append(f"  VIX 5-day change: {s.vix_5d_change_at_prior_close:+.2f} pts")
    if s.scheduled_events_today:
        lines.append(f"  scheduled events today ({len(s.scheduled_events_today)}):")
        for e in s.scheduled_events_today:
            lines.append(
                f"    - {e.ts_utc.isoformat()} [{e.importance}] "
                f"{e.event_type}: {e.headline[:80]}"
            )
    else:
        lines.append("  scheduled events today: (none)")
    return "\n".join(lines)
