"""Idempotency-key tests.

Per `11_execution_layer_spec.md:103-127`. Tests cover the ID format,
the parser, the 24h dedup window, and the gc behavior.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tradegy.execution.idempotency import (
    IdempotencyKeyDeduper,
    OrderRole,
    make_client_order_id,
    parse_client_order_id,
)


def _ts(day: int = 1, hour: int = 12) -> datetime:
    return datetime(2026, 5, day, hour, 0, 0, tzinfo=timezone.utc)


def test_make_client_order_id_format():
    coid = make_client_order_id(
        strategy_id="mes_demo",
        session_date=_ts(),
        intent_seq=3,
        role=OrderRole.ENTRY,
    )
    assert coid == "mes_demo:20260501:3:entry"


def test_make_client_order_id_string_role_works():
    coid = make_client_order_id(
        strategy_id="x",
        session_date=_ts(),
        intent_seq=0,
        role="flatten",
    )
    assert coid == "x:20260501:0:flatten"


def test_make_client_order_id_session_date_uses_utc_date():
    naive_ts = datetime(2026, 5, 1, 23, 30, tzinfo=timezone.utc)
    coid = make_client_order_id(
        strategy_id="x", session_date=naive_ts, intent_seq=0, role=OrderRole.ENTRY,
    )
    assert "20260501" in coid


def test_make_client_order_id_rejects_negative_seq():
    with pytest.raises(ValueError):
        make_client_order_id(
            strategy_id="x", session_date=_ts(), intent_seq=-1, role=OrderRole.ENTRY,
        )


def test_make_client_order_id_rejects_empty_strategy():
    with pytest.raises(ValueError):
        make_client_order_id(
            strategy_id="", session_date=_ts(), intent_seq=0, role=OrderRole.ENTRY,
        )


def test_make_client_order_id_rejects_colon_in_fields():
    with pytest.raises(ValueError):
        make_client_order_id(
            strategy_id="x:y", session_date=_ts(), intent_seq=0, role=OrderRole.ENTRY,
        )


def test_parse_round_trip():
    coid = make_client_order_id(
        strategy_id="mes_demo", session_date=_ts(),
        intent_seq=42, role=OrderRole.STOP,
    )
    parts = parse_client_order_id(coid)
    assert parts == {
        "strategy_id": "mes_demo",
        "session_date": "20260501",
        "intent_seq": "42",
        "role": "stop",
    }


def test_parse_rejects_malformed():
    with pytest.raises(ValueError):
        parse_client_order_id("missing:fields")


def test_dedup_first_register_succeeds():
    d = IdempotencyKeyDeduper()
    assert d.register("x:20260501:0:entry", now=_ts())


def test_dedup_duplicate_within_window_fails():
    d = IdempotencyKeyDeduper()
    d.register("x:20260501:0:entry", now=_ts(hour=12))
    assert not d.register("x:20260501:0:entry", now=_ts(hour=13))


def test_dedup_expires_after_window():
    d = IdempotencyKeyDeduper(window_hours=24)
    d.register("x:20260501:0:entry", now=_ts(day=1, hour=12))
    later = _ts(day=2, hour=13)  # 25h later
    # First register should succeed because the prior entry expired.
    assert d.register("x:20260501:0:entry", now=later)


def test_dedup_has_active():
    d = IdempotencyKeyDeduper()
    d.register("x:20260501:0:entry", now=_ts(hour=12))
    assert d.has_active("x:20260501:0:entry", now=_ts(hour=14))
    later = _ts(day=2, hour=13)
    assert not d.has_active("x:20260501:0:entry", now=later)


def test_role_enum_values():
    assert OrderRole.ENTRY.value == "entry"
    assert OrderRole.STOP.value == "stop"
    assert OrderRole.TARGET.value == "target"
    assert OrderRole.FLATTEN.value == "flatten"
    assert OrderRole.RISK_OVERRIDE.value == "risk_override"
