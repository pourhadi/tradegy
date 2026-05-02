"""Idempotency-key generation + dedup window.

Per `11_execution_layer_spec.md:103-127`. Every order carries a
deterministic, broker-namespace-unique `client_order_id` of the form:

    {strategy_id}:{session_date}:{intent_seq}:{role}

  - strategy_id  — spec id (`04_strategy_spec_schema.md`).
  - session_date — YYYYMMDD UTC of the current CMES session.
  - intent_seq   — monotonic per (strategy, session); resets at each
                   session boundary (matches harness session reset
                   per `05_backtest_harness.md:19`).
  - role         — entry | stop | target | flatten | risk_override.

The dedup window is 24 h: a duplicate `client_order_id` presented
within 24 h is rejected and the prior order's status is returned.
Beyond 24 h, IDs may rotate freely (cross-session collisions are not
possible by construction).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum


_DEDUP_WINDOW_HOURS = 24


class OrderRole(str, Enum):
    ENTRY = "entry"
    STOP = "stop"
    TARGET = "target"
    FLATTEN = "flatten"
    RISK_OVERRIDE = "risk_override"


def make_client_order_id(
    *,
    strategy_id: str,
    session_date: datetime,
    intent_seq: int,
    role: OrderRole | str,
) -> str:
    """Build a client_order_id string per the spec.

    `session_date` may be a full datetime; only the UTC date portion is
    used. `role` accepts either an OrderRole enum or its string value.
    """
    if intent_seq < 0:
        raise ValueError(f"intent_seq must be >= 0 (got {intent_seq})")
    if not strategy_id:
        raise ValueError("strategy_id must be non-empty")
    sd = session_date.astimezone(timezone.utc).strftime("%Y%m%d")
    role_str = role.value if isinstance(role, OrderRole) else str(role)
    if ":" in strategy_id or ":" in role_str:
        raise ValueError(
            "strategy_id and role must not contain ':' (the field separator)"
        )
    return f"{strategy_id}:{sd}:{intent_seq}:{role_str}"


def parse_client_order_id(coid: str) -> dict[str, str]:
    """Inverse of `make_client_order_id`. Returns a dict with the four
    fields. Raises ValueError on malformed input.
    """
    parts = coid.split(":")
    if len(parts) != 4:
        raise ValueError(
            f"client_order_id must have 4 colon-separated fields "
            f"(got {len(parts)}: {coid!r})"
        )
    strategy_id, session_date, intent_seq, role = parts
    return {
        "strategy_id": strategy_id,
        "session_date": session_date,
        "intent_seq": intent_seq,
        "role": role,
    }


@dataclass
class IdempotencyKeyDeduper:
    """Tracks recently-seen `client_order_id` values within a 24h window.

    On a duplicate-within-window, `register` returns False (caller must
    reject the second submission and surface the prior status). Outside
    the window, the entry expires and a fresh registration succeeds.

    The deduper is in-memory only. For multi-process or restart-safe
    operation, persist via `TransitionLog` and rebuild on startup by
    scanning the last 24 h of the log — that's the production path.
    """

    window_hours: int = _DEDUP_WINDOW_HOURS
    _seen: dict[str, datetime] = field(default_factory=dict)

    def register(self, coid: str, *, now: datetime | None = None) -> bool:
        """Try to claim `coid` as a fresh order id. Returns True on
        success (record kept), False on a duplicate inside the window.
        """
        ts = now if now is not None else datetime.now(tz=timezone.utc)
        self._gc(ts)
        if coid in self._seen:
            return False
        self._seen[coid] = ts
        return True

    def has_active(self, coid: str, *, now: datetime | None = None) -> bool:
        """Is `coid` currently inside the live dedup window?"""
        ts = now if now is not None else datetime.now(tz=timezone.utc)
        self._gc(ts)
        return coid in self._seen

    def _gc(self, now: datetime) -> None:
        cutoff = now - timedelta(hours=self.window_hours)
        stale = [k for k, t in self._seen.items() if t < cutoff]
        for k in stale:
            del self._seen[k]
