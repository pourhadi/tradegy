"""Append-only TransitionLog tests.

Verify that records round-trip through disk, that the log preserves
order, and that `replay_until` filters by timestamp inclusively.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from tradegy.execution.lifecycle import (
    OrderState,
    TransitionRecord,
    TransitionSource,
)
from tradegy.execution.log import TransitionLog


def _record(seconds: int, to: OrderState) -> TransitionRecord:
    return TransitionRecord(
        order_id="x:20260501:0:entry",
        from_state=OrderState.PENDING,
        to_state=to,
        ts_utc=datetime(2026, 5, 1, 12, 0, seconds, tzinfo=timezone.utc),
        source=TransitionSource.BROKER,
        reason=f"test_at_{seconds}",
        detail={"k": "v"},
    )


def test_log_append_and_read(tmp_path: Path):
    log = TransitionLog(tmp_path / "transitions.jsonl")
    a = _record(0, OrderState.SUBMITTED)
    b = _record(1, OrderState.WORKING)
    log.append(a)
    log.append(b)

    out = list(log.read_all())
    assert len(out) == 2
    assert out[0].to_state == OrderState.SUBMITTED
    assert out[1].to_state == OrderState.WORKING
    assert out[0].detail == {"k": "v"}


def test_log_handles_unicode_reason(tmp_path: Path):
    log = TransitionLog(tmp_path / "transitions.jsonl")
    rec = TransitionRecord(
        order_id="x:20260501:0:entry",
        from_state=OrderState.PENDING,
        to_state=OrderState.SUBMITTED,
        ts_utc=datetime(2026, 5, 1, tzinfo=timezone.utc),
        source=TransitionSource.LOCAL,
        reason="margin_check_pass — overshoot ≤ 0.5%",
    )
    log.append(rec)
    [out] = list(log.read_all())
    assert out.reason == rec.reason


def test_replay_until_filters_inclusive(tmp_path: Path):
    log = TransitionLog(tmp_path / "transitions.jsonl")
    log.append(_record(0, OrderState.SUBMITTED))
    log.append(_record(1, OrderState.WORKING))
    log.append(_record(2, OrderState.FILLED))

    cutoff = datetime(2026, 5, 1, 12, 0, 1, tzinfo=timezone.utc)
    seen = list(log.replay_until(cutoff))
    assert [r.to_state for r in seen] == [OrderState.SUBMITTED, OrderState.WORKING]


def test_log_creates_path_and_parent_dirs(tmp_path: Path):
    p = tmp_path / "nested" / "subdir" / "transitions.jsonl"
    log = TransitionLog(p)
    assert p.exists()
    log.append(_record(0, OrderState.SUBMITTED))
    assert len(list(log.read_all())) == 1


def test_log_skips_blank_lines(tmp_path: Path):
    p = tmp_path / "transitions.jsonl"
    log = TransitionLog(p)
    log.append(_record(0, OrderState.SUBMITTED))
    # Inject blank lines manually (a corrupted-but-recoverable case).
    with p.open("a") as f:
        f.write("\n\n")
    log.append(_record(1, OrderState.WORKING))

    out = list(log.read_all())
    assert len(out) == 2
