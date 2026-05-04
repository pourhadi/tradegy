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


def aggregate_to_session_daily(bars: pl.DataFrame) -> pl.DataFrame:
    """Collapse 1m bars into one row per session (date) with the
    session's first-open and last-close prices.

    Input: bars with at minimum (ts_utc, open, close).
    Output: (session_date: date, session_open_utc: datetime,
            session_open_price: float, session_close_price: float).

    Sessions are inferred by `(09:30 ET, 16:00 ET)` ranges per UTC
    date — DST-aware. Bars outside any session are dropped.
    """
    if bars.height == 0:
        return pl.DataFrame(schema={
            "session_date": pl.Date,
            "session_open_utc": pl.Datetime("ns", "UTC"),
            "session_open_price": pl.Float64,
            "session_close_price": pl.Float64,
        })
    # Compute per-bar session date by translating ts_utc to ET, taking
    # date. (We then filter to bars that fall in 09:30-16:00 ET.)
    bars2 = bars.sort("ts_utc").with_columns(
        pl.col("ts_utc").dt.convert_time_zone("America/New_York").dt.date()
            .alias("__session_date"),
        # Cast hour/minute to Int64 BEFORE arithmetic — polars returns
        # Int8 by default and `h * 60` overflows for h > 2.
        pl.col("ts_utc").dt.convert_time_zone("America/New_York").dt.hour()
            .cast(pl.Int64).alias("__hour_et"),
        pl.col("ts_utc").dt.convert_time_zone("America/New_York").dt.minute()
            .cast(pl.Int64).alias("__minute_et"),
    ).filter(
        # Within RTH: 09:30 ET to 16:00 ET (16:00 itself excluded since
        # bars are right-labeled; 16:00 bar covers 15:59-16:00).
        (pl.col("__hour_et") * 60 + pl.col("__minute_et") >= 9 * 60 + 30)
        & (pl.col("__hour_et") * 60 + pl.col("__minute_et") < 16 * 60)
    )
    if bars2.height == 0:
        return pl.DataFrame(schema={
            "session_date": pl.Date,
            "session_open_utc": pl.Datetime("ns", "UTC"),
            "session_open_price": pl.Float64,
            "session_close_price": pl.Float64,
        })
    grouped = bars2.group_by("__session_date").agg(
        pl.col("ts_utc").first().alias("session_open_utc"),
        pl.col("open").first().alias("session_open_price"),
        pl.col("close").last().alias("session_close_price"),
    ).rename({"__session_date": "session_date"}).sort("session_date")
    return grouped


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
    bars: pl.DataFrame | None = None,
    session_daily: pl.DataFrame | None = None,
    events: pl.DataFrame | None = None,
    vix_close_df: pl.DataFrame | None = None,
    vix_pctile_df: pl.DataFrame | None = None,
    vix_5d_df: pl.DataFrame | None = None,
) -> SessionPreOpenSnapshot:
    """Build the sealed pre-open snapshot for one session.

    Performance: pass pre-loaded `bars`, `session_daily`, `events`,
    and the three VIX feature frames to avoid re-reading multi-
    million-row parquets on every call. Bulk callers (the labeling
    CLI) should load once and reuse across sessions; for prior-N-day
    return computation, `session_daily` (one row per session) gives
    O(log N) lookup vs O(N) full-frame filter.
    """
    if bars is None:
        bars = _read_bars(instrument, raw_root=raw_root)
    if session_daily is None:
        session_daily = aggregate_to_session_daily(bars)
    open_utc, close_utc = _session_open_close_utc(session_date)

    # Today's session open (use session_daily for O(log N) lookup;
    # falls back to a per-bar filter if session_daily is empty).
    if session_daily.height > 0:
        today_row = session_daily.filter(
            pl.col("session_date") == session_date
        )
        today_open = (
            float(today_row.row(0, named=True)["session_open_price"])
            if today_row.height > 0 else None
        )
    else:
        today_open_bar = _bar_at_or_after(bars, open_utc)
        today_open = (
            float(today_open_bar["open"]) if today_open_bar is not None else None
        )

    # Prior session close = last close from session_daily before
    # session_date.
    if session_daily.height > 0:
        prior_row = session_daily.filter(
            pl.col("session_date") < session_date
        ).sort("session_date").tail(1)
        prior_close = (
            float(prior_row.row(0, named=True)["session_close_price"])
            if prior_row.height > 0 else None
        )
    else:
        prior_bar = _bar_at_or_before(bars, open_utc - timedelta(seconds=1))
        prior_close = (
            float(prior_bar["close"]) if prior_bar is not None else None
        )

    overnight_gap_pct: float | None = None
    if today_open is not None and prior_close not in (None, 0):
        overnight_gap_pct = (today_open - prior_close) / prior_close

    # Prior-5-day close-to-close % returns. Use session_daily (one row
    # per session) for O(log N) lookup; pick the 6 most-recent sessions
    # before today and compute pairwise % moves.
    if session_daily.height > 0:
        prior_sessions = session_daily.filter(
            pl.col("session_date") < session_date
        ).sort("session_date").tail(6)
        closes = prior_sessions["session_close_price"].to_list()
        moves: list[float] = []
        for i in range(1, len(closes)):
            if closes[i - 1] != 0:
                moves.append((closes[i] - closes[i - 1]) / closes[i - 1])
        # `moves` is oldest→newest. Reverse so the most-recent move is first.
        prior_5d = list(reversed(moves))[:5]
    else:
        prior_5d = []

    # VIX features as of prior close. Lazy-load if not pre-supplied.
    if vix_close_df is None:
        vix_close_df = _read_feature_value("vix_daily_close", feature_root=feature_root)
    if vix_pctile_df is None:
        vix_pctile_df = _read_feature_value("vix_daily_pctile_252", feature_root=feature_root)
    if vix_5d_df is None:
        vix_5d_df = _read_feature_value("vix_daily_5d_change", feature_root=feature_root)
    vix_at_prior = _value_as_of(vix_close_df, open_utc - timedelta(seconds=1))
    vix_pctile_at_prior = _value_as_of(vix_pctile_df, open_utc - timedelta(seconds=1))
    vix_5d_at_prior = _value_as_of(vix_5d_df, open_utc - timedelta(seconds=1))

    # Scheduled events for today (UTC date span).
    if events is None:
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
