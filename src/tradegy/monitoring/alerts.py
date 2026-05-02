"""Alert router with severity-matrix dispatch + dedup window.

Per `12_live_monitoring_spec.md` § Escalation chain (§112-122):

  INFO     — logged only (no notification)
  WARNING  — operator alert, dedup within 15 min
  CRITICAL — operator alert, re-fire every 5 min until acknowledged

The router itself is dumb — it just dispatches results to handlers
registered per severity, with a per-severity cooldown window. The
actual notification mechanics (push, SMS, dashboard) live in the
handlers, registered by the operational config.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tradegy.monitoring.types import HealthCheckResult, Severity


_log = logging.getLogger(__name__)


# Default per-severity cooldown windows (seconds). Production wiring
# may override; defaults match doc 12 §117-118.
DEFAULT_COOLDOWNS: dict[Severity, float] = {
    Severity.INFO: 0.0,
    Severity.WARNING: 15 * 60.0,    # 15 min
    Severity.CRITICAL: 5 * 60.0,    # 5 min
}


AlertHandler = Callable[[HealthCheckResult], None]


@dataclass
class AlertRouter:
    """Dispatches non-OK HealthCheckResults to severity-specific
    handlers, with a per-dedup-key cooldown.

    Multiple handlers per severity are allowed; they fire in
    registration order. A handler raising an exception does not stop
    other handlers from firing — exceptions are logged.

    The cooldown is keyed by `result.effective_dedup_key`. Within the
    cooldown window, the same key is suppressed. Outside the window,
    it fires again.
    """

    cooldowns: dict[Severity, float] = field(
        default_factory=lambda: dict(DEFAULT_COOLDOWNS)
    )
    _handlers: dict[Severity, list[AlertHandler]] = field(default_factory=dict)
    _last_fired: dict[str, datetime] = field(default_factory=dict)

    def add_handler(self, severity: Severity, handler: AlertHandler) -> None:
        self._handlers.setdefault(severity, []).append(handler)

    def route(self, result: HealthCheckResult) -> bool:
        """Route a result through the matrix.

        Returns True if any handler was called (i.e., not deduped),
        False if suppressed by the cooldown.
        """
        if result.severity == Severity.OK:
            return False

        key = result.effective_dedup_key
        cooldown_s = self.cooldowns.get(result.severity, 0.0)
        last = self._last_fired.get(key)
        if (
            last is not None
            and cooldown_s > 0
            and (result.ts_utc - last) < timedelta(seconds=cooldown_s)
        ):
            return False  # within cooldown — suppress

        self._last_fired[key] = result.ts_utc
        handlers = self._handlers.get(result.severity, [])
        for h in handlers:
            try:
                h(result)
            except Exception as exc:  # noqa: BLE001
                _log.error(
                    "alert handler raised on %s [%s]: %r",
                    result.check_id, result.severity.value, exc,
                )
        return True
