"""Reconciliation loop — periodic local-vs-broker divergence dispatch.

Per `11_execution_layer_spec.md:214-237`. Runs continuously while the
live system is connected. Cadences (§218-223):

  Every 1 s    — open-order state diff
  Every 5 s    — position quantity diff per instrument
  Every 30 s   — account balance / available funds / margin diff
  Every 60 s   — realised P&L since session open

Each cadence calls the corresponding `BrokerRouter.query_*` method,
runs the relevant `divergence.detect_*` function, and dispatches each
detected event to two registered handlers:

  * `divergence_handler` — called on EVERY event (logging, alerting,
    governance audit trail).
  * `escalation_handler` — called only on CRITICAL events (kill-switch
    trip, session-flatten, etc.).

The loop is structured so the cadence-driven `tick(now, ...)` method
is a pure-ish state machine — it inspects an injected `now` and
decides which checks are due, runs only those, and updates internal
last-run timestamps. Real-time scheduling lives in `run_forever()`,
which calls `tick()` from an `asyncio.sleep`-driven outer loop.

This split makes tests deterministic: tests advance `now` by hand and
assert on what the tick decided to run; only one integration test
needs to exercise the actual asyncio.sleep path.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from tradegy.execution.divergence import (
    DivergenceEvent,
    DivergenceSeverity,
    detect_account_divergences,
    detect_order_divergences,
    detect_position_divergences,
)
from tradegy.execution.router import BrokerRouter


_log = logging.getLogger(__name__)


class CheckType(str, Enum):
    OPEN_ORDERS = "open_orders"
    POSITIONS = "positions"
    ACCOUNT = "account"
    PNL = "pnl"


# Default cadences (seconds) per doc 11 §218-223.
DEFAULT_CADENCES: dict[CheckType, float] = {
    CheckType.OPEN_ORDERS: 1.0,
    CheckType.POSITIONS: 5.0,
    CheckType.ACCOUNT: 30.0,
    CheckType.PNL: 60.0,
}


# Handler signatures.
DivergenceHandler = Callable[[DivergenceEvent], None]
EscalationHandler = Callable[[DivergenceEvent], None]
LocalStateProvider = Callable[
    [], tuple[dict[str, Any], dict[str, int]]
]
"""Returns `(local_orders_by_coid, local_positions_by_instrument)` —
the loop's view of local state at the moment the tick runs. The
caller (e.g. the live system orchestrator) maintains these and feeds
them in. Keeps the loop broker-agnostic and decoupled from how local
state is tracked.
"""


@dataclass
class TickReport:
    """Per-tick summary returned by `tick()`. Lets the caller log
    activity and tests assert on what ran.
    """

    ts_utc: datetime
    checks_run: list[CheckType] = field(default_factory=list)
    events: list[DivergenceEvent] = field(default_factory=list)


class ReconciliationLoop:
    """Schedules periodic divergence checks against a BrokerRouter.

    Construction is cheap; nothing happens until `tick()` or
    `run_forever()` is called. Both pathways drive the same cadence
    logic — the only difference is who advances the clock.
    """

    def __init__(
        self,
        *,
        router: BrokerRouter,
        local_state_provider: LocalStateProvider,
        divergence_handler: DivergenceHandler,
        escalation_handler: EscalationHandler,
        cadences: dict[CheckType, float] | None = None,
    ) -> None:
        self._router = router
        self._local_state = local_state_provider
        self._divergence_handler = divergence_handler
        self._escalation_handler = escalation_handler
        self._cadences = dict(cadences or DEFAULT_CADENCES)
        self._last_run: dict[CheckType, datetime | None] = {
            ct: None for ct in CheckType
        }

    async def tick(self, *, now: datetime) -> TickReport:
        """One pass through the schedule. Inspects `now` against the
        last-run timestamps and runs every check whose cadence has
        elapsed. Returns a TickReport listing which checks ran and
        every divergence dispatched.
        """
        report = TickReport(ts_utc=now)

        if self._is_due(CheckType.OPEN_ORDERS, now):
            broker_orders = await self._router.query_open_orders()
            local_orders, _ = self._local_state()
            events = detect_order_divergences(
                local_orders=local_orders, broker_orders=broker_orders,
            )
            self._dispatch(events, report)
            self._last_run[CheckType.OPEN_ORDERS] = now
            report.checks_run.append(CheckType.OPEN_ORDERS)

        if self._is_due(CheckType.POSITIONS, now):
            broker_positions = await self._router.query_positions()
            _, local_positions = self._local_state()
            events = detect_position_divergences(
                local_positions=local_positions,
                broker_positions=broker_positions,
            )
            self._dispatch(events, report)
            self._last_run[CheckType.POSITIONS] = now
            report.checks_run.append(CheckType.POSITIONS)

        if self._is_due(CheckType.ACCOUNT, now):
            account = await self._router.query_account()
            events = detect_account_divergences(broker_account=account)
            self._dispatch(events, report)
            self._last_run[CheckType.ACCOUNT] = now
            report.checks_run.append(CheckType.ACCOUNT)

        if self._is_due(CheckType.PNL, now):
            # PnL cadence is a placeholder for the dedicated session-PnL
            # divergence check that lands when realized P&L tracking is
            # wired to the harness session model. For now it's a no-op
            # but the cadence timestamps are advanced so the orchestrator
            # respects the schedule.
            self._last_run[CheckType.PNL] = now
            report.checks_run.append(CheckType.PNL)

        return report

    async def run_forever(
        self,
        *,
        sleep_seconds: float = 0.5,
        max_ticks: int | None = None,
    ) -> None:
        """Production runner. Calls `tick()` on a fixed sleep cadence
        until cancelled. `sleep_seconds` should be at most half of the
        finest cadence (default 1s open-orders → 0.5s sleep).

        Tests pass `max_ticks` to bound the run; production leaves it
        as None for an unbounded loop terminated by asyncio.cancel.
        """
        ticks = 0
        while True:
            now = datetime.now(tz=timezone.utc)
            try:
                await self.tick(now=now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _log.error("reconciliation tick failed: %r", exc)
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                return
            await asyncio.sleep(sleep_seconds)

    def _is_due(self, check: CheckType, now: datetime) -> bool:
        last = self._last_run[check]
        if last is None:
            return True
        cadence_s = self._cadences[check]
        return (now - last) >= timedelta(seconds=cadence_s)

    def _dispatch(
        self, events: list[DivergenceEvent], report: TickReport
    ) -> None:
        for e in events:
            report.events.append(e)
            try:
                self._divergence_handler(e)
            except Exception as exc:  # noqa: BLE001
                _log.error("divergence_handler raised: %r", exc)
            if e.severity == DivergenceSeverity.CRITICAL:
                try:
                    self._escalation_handler(e)
                except Exception as exc:  # noqa: BLE001
                    _log.error("escalation_handler raised: %r", exc)
