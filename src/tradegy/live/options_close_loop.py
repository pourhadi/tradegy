"""Close-side automation for the live-options orchestrator (V2).

Three concerns wired together:

  1. Broker-position fetch — query IBKR for current option positions
     scoped to the configured underlying (e.g. SPY), grouped by
     contract.
  2. Reconciliation — match each broker position against the
     persisted local registry. Flag divergence (broker has a leg
     we don't know about, or registry says we hold something the
     broker doesn't show).
  3. Close-trigger evaluation — for each registered (and broker-
     confirmed) position, run `should_close` against today's chain
     snapshot. Route a closing combo via IbkrOptionsRouter for
     each triggered position; record `close` rows in the registry.

Design constraints:
  - Reconciliation runs FIRST. If divergence is detected, we do
    NOT auto-close (could be operator manually closed via TWS, or
    an assignment we missed). Surface the divergence; operator
    investigates.
  - Close routing uses idempotent client_order_ids derived from
    `make_client_order_id(strategy_id, session_date, intent_seq,
    role=FLATTEN)`. Re-running the same session collapses to
    same coid; broker rejects duplicates.

Per `14_options_volatility_selling.md` Phase E. The V1 entry-only
orchestrator (commit 5d1b9cb) intentionally deferred this; V2
closes the discipline gap so the operator doesn't have to track
50%/21DTE/200% triggers manually.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from tradegy.execution.ibkr_options_router import IbkrOptionsRouter
from tradegy.execution.idempotency import OrderRole, make_client_order_id
from tradegy.live.options_position_registry import (
    append_close,
    load_open_positions,
)
from tradegy.options.chain import ChainSnapshot, OptionSide
from tradegy.options.positions import LegOrder, MultiLegOrder, MultiLegPosition
from tradegy.options.strategy import ManagementRules, should_close


_log = logging.getLogger(__name__)


# ── Broker-position fetch ─────────────────────────────────────────


@dataclass(frozen=True)
class BrokerOptionLeg:
    """One option leg as reported by IBKR's positions endpoint.

    Mirrors the fields ib_async exposes on Position objects when
    contract.secType == "OPT". Quantity is signed (positive long,
    negative short) to match our internal convention.
    """

    underlying: str
    expiry: date
    strike: float
    side: OptionSide
    quantity: int
    avg_cost: float          # per share — IBKR convention is per share, not per contract


def fetch_broker_option_legs(
    ib: Any, *, underlying: str,
) -> list[BrokerOptionLeg]:
    """Query IBKR for current option positions on `underlying`.

    `ib` is an `ib_async.IB` instance (already connected). Returns
    one leg per (expiry, strike, side) combination — multi-leg
    combos appear as multiple BrokerOptionLeg entries.
    """
    raw_positions = ib.positions()
    out: list[BrokerOptionLeg] = []
    for pos in raw_positions:
        contract = getattr(pos, "contract", None)
        if contract is None or getattr(contract, "secType", None) != "OPT":
            continue
        if getattr(contract, "symbol", None) != underlying:
            continue
        # IBKR's lastTradeDateOrContractMonth on options is YYYYMMDD.
        ltd = getattr(contract, "lastTradeDateOrContractMonth", "")
        if not ltd:
            continue
        try:
            expiry = date(int(ltd[:4]), int(ltd[4:6]), int(ltd[6:8]))
        except (ValueError, IndexError):
            continue
        right = getattr(contract, "right", None)
        if right == "C":
            side = OptionSide.CALL
        elif right == "P":
            side = OptionSide.PUT
        else:
            continue
        strike = float(getattr(contract, "strike", 0.0))
        qty = int(getattr(pos, "position", 0))
        avg_cost = float(getattr(pos, "avgCost", 0.0))
        # IBKR avgCost on options is per CONTRACT, not per share.
        # Divide by 100 (or whatever multiplier) to normalize to
        # per-share. We use the contract.multiplier which IBKR
        # populates for options.
        mult = int(float(getattr(contract, "multiplier", 100) or 100))
        per_share_cost = avg_cost / mult if mult > 0 else avg_cost
        out.append(BrokerOptionLeg(
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            side=side,
            quantity=qty,
            avg_cost=per_share_cost,
        ))
    return out


# ── Reconciliation ────────────────────────────────────────────────


@dataclass
class ReconciliationReport:
    """Result of comparing the local registry against IBKR positions.

    `matched` — registry positions whose every leg appears in the
    broker positions with matching qty.
    `local_only` — registered positions whose legs are NOT all
    present in broker positions. Likely causes: broker manual close
    via TWS, assignment, partial fill that ate one leg.
    `broker_only` — broker positions whose legs are NOT in the
    registry. Likely causes: positions opened outside this system,
    or a registry-write failure.
    """

    matched: list[MultiLegPosition] = field(default_factory=list)
    local_only: list[MultiLegPosition] = field(default_factory=list)
    broker_only: list[BrokerOptionLeg] = field(default_factory=list)

    @property
    def has_divergence(self) -> bool:
        return bool(self.local_only) or bool(self.broker_only)


def reconcile(
    *,
    local_positions: list[MultiLegPosition],
    broker_legs: list[BrokerOptionLeg],
) -> ReconciliationReport:
    """Match each registered position's legs against broker legs.

    Matching key per leg: (underlying, expiry, strike, side). The
    quantity match accounts for `contracts` × `leg.quantity` —
    a 1-lot iron condor with 4 legs at qty ±1 each matches broker
    legs at qty ±1; a 5-lot version matches broker legs at qty ±5.

    Broker legs that match are consumed from the working pool to
    avoid double-matching when two registered positions share an
    expiry/strike/side. Order of matching is deterministic
    (registry order); for ambiguous chains add a position_id
    suffix on the broker side via `orderRef` in V3.
    """
    report = ReconciliationReport()
    remaining_legs: dict[tuple[str, date, float, str], int] = {}
    for leg in broker_legs:
        key = (leg.underlying, leg.expiry, leg.strike, leg.side.value)
        remaining_legs[key] = remaining_legs.get(key, 0) + leg.quantity

    for pos in local_positions:
        all_matched = True
        consume: list[tuple[tuple[str, date, float, str], int]] = []
        for leg in pos.legs:
            key = (leg.underlying, leg.expiry, leg.strike, leg.side.value)
            needed_qty = leg.quantity * pos.contracts
            available = remaining_legs.get(key, 0)
            # Sign matters — short legs (negative needed_qty) match
            # short broker legs (negative available).
            if needed_qty == 0:
                continue
            if needed_qty > 0 and available >= needed_qty:
                consume.append((key, needed_qty))
            elif needed_qty < 0 and available <= needed_qty:
                consume.append((key, needed_qty))
            else:
                all_matched = False
                break
        if all_matched:
            for key, qty in consume:
                remaining_legs[key] = remaining_legs[key] - qty
            report.matched.append(pos)
        else:
            report.local_only.append(pos)

    # Whatever's left in remaining_legs is broker-only.
    for key, qty in remaining_legs.items():
        if qty == 0:
            continue
        underlying, expiry, strike, side_str = key
        report.broker_only.append(BrokerOptionLeg(
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            side=OptionSide(side_str),
            quantity=qty,
            avg_cost=0.0,  # we only have aggregate qty here
        ))
    return report


# ── Close-trigger evaluation ──────────────────────────────────────


@dataclass
class CloseDecision:
    """One close to route. The closing combo INVERTS each leg's
    quantity sign: shorting a credit spread to open means buying
    it back to close.
    """

    position: MultiLegPosition
    close_reason: str

    def to_close_order(self) -> MultiLegOrder:
        legs = tuple(
            LegOrder(
                expiry=leg.expiry,
                strike=leg.strike,
                side=leg.side,
                quantity=-leg.quantity,
            )
            for leg in self.position.legs
        )
        return MultiLegOrder(
            tag=f"close:{self.position.position_id}",
            contracts=self.position.contracts,
            legs=legs,
        )


def evaluate_closes(
    *,
    positions: list[MultiLegPosition],
    snapshot: ChainSnapshot,
    rules: ManagementRules,
) -> list[CloseDecision]:
    """Run `should_close` against each position; return triggered
    closes.

    Identical semantics to the backtest runner's per-snapshot
    management pass, but applied to live positions instead of
    backtest-replay positions.
    """
    out: list[CloseDecision] = []
    for pos in positions:
        reason = should_close(pos, snapshot, rules)
        if reason is None:
            continue
        out.append(CloseDecision(position=pos, close_reason=reason))
    return out


# ── Close routing (CLI glue) ──────────────────────────────────────


@dataclass
class CloseRouteResult:
    position_id: str
    client_order_id: str
    close_reason: str
    accepted: bool
    broker_order_id: str | None = None
    error: str | None = None
    submitted_ts: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc),
    )


async def route_close_decisions(
    *,
    closes: list[CloseDecision],
    snapshot: ChainSnapshot,
    router: IbkrOptionsRouter,
    session_date: datetime,
    registry_root: Any = None,
) -> list[CloseRouteResult]:
    """Place each close as a multi-leg combo via the router; await
    fill confirmation; record `close` in registry only on FILLED.

    Caller owns the IB connection — this function expects an
    already-connected `IbkrOptionsRouter` so the same connection
    can be reused for the entry routing in the same session.

    Fill semantics (mirrors entry routing per V3a):
      FILLED → registry close written, accepted=True.
      REJECTED/CANCELLED/EXPIRED → not accepted, no registry side
        effect; position remains "open" in registry. Next session
        will re-attempt the close.
      WORKING after timeout → cancel + record as not accepted; same
        re-attempt-next-session semantics.
    """
    from tradegy.execution.lifecycle import OrderState, TERMINAL_STATES
    from tradegy.live.options_routing import await_terminal_state

    results: list[CloseRouteResult] = []
    for intent_seq, dec in enumerate(closes):
        order = dec.to_close_order()
        coid = make_client_order_id(
            strategy_id=dec.position.strategy_class,
            session_date=session_date,
            intent_seq=intent_seq,
            role=OrderRole.FLATTEN,
        )
        try:
            managed = await router.place_combo(
                order=order, snapshot=snapshot, client_order_id=coid,
            )
            final_state = await await_terminal_state(
                router=router, client_order_id=coid,
            )
            if final_state == OrderState.FILLED:
                append_close(
                    position_id=dec.position.position_id,
                    closed_ts=datetime.now(tz=timezone.utc),
                    closed_reason=dec.close_reason,
                    broker_close_order_id=managed.broker_order_id,
                    root=registry_root,
                )
                results.append(CloseRouteResult(
                    position_id=dec.position.position_id,
                    client_order_id=coid,
                    close_reason=dec.close_reason,
                    accepted=True,
                    broker_order_id=managed.broker_order_id,
                ))
            elif final_state in TERMINAL_STATES:
                results.append(CloseRouteResult(
                    position_id=dec.position.position_id,
                    client_order_id=coid,
                    close_reason=dec.close_reason,
                    accepted=False,
                    broker_order_id=managed.broker_order_id,
                    error=f"close ended in {final_state.value} not FILLED",
                ))
            else:
                # Cancel non-terminal-after-timeout — don't leave a
                # close limit hanging.
                try:
                    await router.cancel_combo(coid)
                except Exception:  # noqa: BLE001
                    _log.exception(
                        "cancel after close-fill timeout failed for %s", coid,
                    )
                results.append(CloseRouteResult(
                    position_id=dec.position.position_id,
                    client_order_id=coid,
                    close_reason=dec.close_reason,
                    accepted=False,
                    broker_order_id=managed.broker_order_id,
                    error=(
                        f"close in {final_state.value} after fill-wait "
                        "timeout — cancelled; will retry next session"
                    ),
                ))
        except Exception as exc:  # noqa: BLE001
            _log.exception(
                "close routing failed for position %s",
                dec.position.position_id,
            )
            results.append(CloseRouteResult(
                position_id=dec.position.position_id,
                client_order_id=coid,
                close_reason=dec.close_reason,
                accepted=False,
                error=f"{type(exc).__name__}: {exc}",
            ))
    return results
