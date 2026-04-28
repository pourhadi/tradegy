"""Exchange-calendar wrapper for session-aware audit gating.

The basic audit's `excessive_gap` check is naive — it flags every inter-row
gap above a threshold. For a 9-year continuous CME futures dataset this
fires on every overnight maintenance break (16:00–17:00 CT), every weekend
(Fri 16:00 CT → Sun 17:00 CT), and every CME holiday. None of those are
real anomalies; they're calendar-driven non-sessions.

This module computes the expected non-session intervals over a coverage
window. It combines two sources:

1. ``exchange_calendars`` weekend + holiday gaps (between session_close
   and the next session_open).
2. Daily CME maintenance halt 16:00–17:00 America/Chicago for Mon-Thu of
   every week — `exchange_calendars`'s CME / CMES calendars treat sessions
   as contiguous and don't model this halt. We add it explicitly,
   tz-converted per-day so DST is handled correctly.

The CME-specific halt logic only kicks in for the ``CME`` and ``CMES``
calendar names. Other calendars only get the inter-session gaps.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import exchange_calendars as xc


_CME_TZ = ZoneInfo("America/Chicago")
_CME_CALENDAR_NAMES = frozenset({"CME", "CMES"})
_CME_HALT_START = time(16, 0)  # 16:00 CT — CME E-mini daily close
_CME_HALT_END = time(17, 0)  # 17:00 CT — next session open


def _exchange_inter_session_gaps(
    calendar_name: str, start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    cal = xc.get_calendar(calendar_name)
    pad = timedelta(days=1)
    sessions = cal.sessions_in_range(
        (start - pad).date().isoformat(),
        (end + pad).date().isoformat(),
    )
    intervals: list[tuple[datetime, datetime]] = []
    prev_close: datetime | None = None
    for sess in sessions:
        sess_open = cal.session_open(sess).to_pydatetime().astimezone(timezone.utc)
        sess_close = cal.session_close(sess).to_pydatetime().astimezone(timezone.utc)
        if prev_close is not None and sess_open > prev_close:
            intervals.append((prev_close, sess_open))
        prev_close = sess_close
    return intervals


def _cme_daily_halts(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Daily 16:00 CT → 17:00 CT halt for Mon-Thu within [start, end].

    Computed per-day in CT then converted to UTC — DST and standard time
    transitions are handled implicitly by the zoneinfo conversion.
    """
    intervals: list[tuple[datetime, datetime]] = []
    cur = start.date()
    end_date = end.date()
    while cur <= end_date:
        if cur.weekday() in (0, 1, 2, 3):  # Mon-Thu
            halt_start_ct = datetime.combine(cur, _CME_HALT_START, tzinfo=_CME_TZ)
            halt_end_ct = datetime.combine(cur, _CME_HALT_END, tzinfo=_CME_TZ)
            intervals.append(
                (
                    halt_start_ct.astimezone(timezone.utc),
                    halt_end_ct.astimezone(timezone.utc),
                )
            )
        cur += timedelta(days=1)
    return intervals


def expected_non_session_intervals(
    calendar_name: str, start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    """Return the UTC intervals of non-session time within [start, end].

    Combines ``exchange_calendars`` inter-session gaps (weekends, holidays)
    with the CME daily maintenance halt for ``CME`` / ``CMES``. The result
    is sorted by start.
    """
    intervals = _exchange_inter_session_gaps(calendar_name, start, end)
    if calendar_name in _CME_CALENDAR_NAMES:
        intervals.extend(_cme_daily_halts(start, end))
    intervals = [(s, e) for s, e in intervals if e > start and s < end]
    intervals.sort(key=lambda iv: iv[0])
    return intervals


def is_expected_gap(
    gap_start: datetime,
    gap_end: datetime,
    intervals: list[tuple[datetime, datetime]],
) -> bool:
    """Is the observed gap (gap_start, gap_end) entirely inside an expected
    non-session interval?
    """
    # Linear scan — intervals.count is small (~2300 for 9 years CME).
    for iv_start, iv_end in intervals:
        if iv_start <= gap_start and gap_end <= iv_end:
            return True
    return False


__all__ = ["expected_non_session_intervals", "is_expected_gap"]
