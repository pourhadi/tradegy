"""Route a `LiveDecision`'s entry candidates to IBKR paper.

Glue between `tradegy.live.options_orchestrator.LiveDecision`
(pure-data decision artifact) and
`tradegy.execution.ibkr_options_router.IbkrOptionsRouter` (the
multi-leg combo router that talks to ib_async).

Per `14_options_volatility_selling.md` Phase E paper-trade.
Connection lifecycle is owned by `tradegy.live.ibkr.IBKRConnection`
which reads IBKR_HOST / IBKR_PORT / IBKR_CLIENT_ID from the env
(paper TWS defaults: 127.0.0.1:7497, client_id 17). The operator
must have TWS or IB Gateway running and logged into the paper
account before calling `route_decision`.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from tradegy.execution.ibkr_options_router import IbkrOptionsRouter
from tradegy.execution.idempotency import OrderRole, make_client_order_id
from tradegy.execution.lifecycle import OrderState, TERMINAL_STATES
from tradegy.live.ibkr import IBKRConnection
from tradegy.live.options_position_registry import (
    PersistedLeg,
    append_open,
)
from tradegy.options.chain import ChainSnapshot, OptionLeg, OptionSide
from tradegy.options.chain_io import iter_chain_snapshots
from tradegy.options.cost_model import OptionCostModel
from tradegy.options.positions import (
    LegOrder,
    MultiLegOrder,
    OptionPosition,
    compute_max_loss_per_contract,
)


_log = logging.getLogger(__name__)


@dataclass
class RouteResult:
    """Outcome of routing one entry candidate to IBKR.

    `accepted` is True when the order PLACEMENT succeeded (IBKR
    acknowledged). `final_state` reflects the FSM state at the
    end of the fill-confirmation poll: FILLED is the only state
    that counts as "actually traded"; WORKING after timeout means
    the limit didn't fill and the order was cancelled; REJECTED
    means the broker refused.

    A registry-write happens ONLY on FILLED. WORKING-then-cancelled
    and REJECTED produce a RouteResult with `final_state` set but
    no registry side effect — re-running the next session can
    re-attempt with a fresh strategy decision.
    """

    strategy_id: str
    client_order_id: str
    accepted: bool
    broker_order_id: str | None = None
    error: str | None = None
    final_state: str | None = None
    submitted_ts: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


def reconstruct_chain_snapshot_for_decision(
    *,
    source_id: str,
    ticker: str,
    snapshot_ts_utc: datetime,
) -> ChainSnapshot:
    """Re-load the ChainSnapshot the decision was generated against.

    The `LiveDecision` only stores summary metadata about the
    snapshot, not the full chain — we re-load from parquet so
    the router can look up each leg's quote at the same prices
    the strategy saw.

    Returns the snapshot whose `ts_utc` matches the recorded
    `snapshot_ts_utc`. Raises if not present (decision file
    is stale relative to the ingested data).
    """
    snaps = list(iter_chain_snapshots(source_id, ticker=ticker))
    for s in snaps:
        if s.ts_utc == snapshot_ts_utc:
            return s
    raise RuntimeError(
        f"snapshot_ts_utc={snapshot_ts_utc.isoformat()} not present in "
        f"source={source_id!r} ticker={ticker!r}; the decision file is "
        "stale or the source was re-ingested without the original date"
    )


def _entry_dict_to_order(entry: dict[str, Any]) -> MultiLegOrder:
    """Re-hydrate a serialized entry dict back into a MultiLegOrder."""
    legs = tuple(
        LegOrder(
            expiry=date.fromisoformat(leg["expiry"]),
            strike=float(leg["strike"]),
            side=OptionSide(leg["side"]),
            quantity=int(leg["quantity"]),
        )
        for leg in entry["legs"]
    )
    return MultiLegOrder(
        tag=entry["tag"],
        contracts=int(entry["contracts"]),
        legs=legs,
    )


async def route_decision(
    *,
    decision_entries: list[dict[str, Any]],
    snapshot: ChainSnapshot,
    paper_account: str,
    session_date: datetime,
) -> list[RouteResult]:
    """Connect to IBKR paper, place each entry as a multi-leg combo,
    return per-entry RouteResults.

    Idempotency: each combo's client_order_id is built via
    `make_client_order_id(strategy_id, session_date, intent_seq,
    role=ENTRY)`. Re-running the same decision on the same session
    produces identical coids — IBKR will reject duplicates rather
    than placing twice.

    Connection: lives only within this call. Connect → place each
    entry → disconnect.
    """
    conn = IBKRConnection()
    await conn.connect()
    try:
        # Belt-and-suspenders: log the actual broker account-id we
        # connected to so the audit trail confirms paper.
        try:
            accounts = conn.ib.managedAccounts()
        except Exception:  # noqa: BLE001
            accounts = []
        _log.info(
            "ibkr connected: host=%s port=%s accounts=%s",
            conn.host, conn.port, accounts,
        )
        if paper_account not in accounts:
            raise RuntimeError(
                f"paper_account={paper_account!r} not in connected "
                f"accounts={accounts!r}; aborting before any order placed"
            )

        router = IbkrOptionsRouter(ib=conn.ib)
        await router.connect()

        cost = OptionCostModel()
        results: list[RouteResult] = []
        for intent_seq, entry in enumerate(decision_entries):
            order = _entry_dict_to_order(entry)
            coid = make_client_order_id(
                strategy_id=entry["strategy_id"],
                session_date=session_date,
                intent_seq=intent_seq,
                role=OrderRole.ENTRY,
            )
            try:
                managed = await router.place_combo(
                    order=order,
                    snapshot=snapshot,
                    client_order_id=coid,
                )
                # Persist routed entry so V2 close loop can manage
                # it on subsequent sessions. Compute entry-credit
                # and max-loss the same way the runner does so the
                # downstream should_close math matches the backtest.
                _persist_entry(
                    position_id=coid,
                    strategy_id=entry["strategy_id"],
                    order=order,
                    snapshot=snapshot,
                    cost=cost,
                    broker_order_id=managed.broker_order_id,
                )
                results.append(RouteResult(
                    strategy_id=entry["strategy_id"],
                    client_order_id=coid,
                    accepted=True,
                    broker_order_id=managed.broker_order_id,
                ))
            except Exception as exc:  # noqa: BLE001
                # Per-entry resilience: if leg-2 fails we still
                # record the failure and continue to leg-3. Routing
                # is per-combo; partial-portfolio fills are valid.
                _log.exception("place_combo failed for %s", entry["strategy_id"])
                results.append(RouteResult(
                    strategy_id=entry["strategy_id"],
                    client_order_id=coid,
                    accepted=False,
                    error=f"{type(exc).__name__}: {exc}",
                ))
        return results
    finally:
        await conn.disconnect()


async def await_terminal_state(
    *,
    router: IbkrOptionsRouter,
    client_order_id: str,
    timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 0.5,
) -> OrderState:
    """Poll the router's tracked ManagedOrder until terminal or
    timeout. Returns the final OrderState.

    Why polling not events: the router already wires Trade events
    to the FSM via _wire_trade_events; the side-effect of those
    events is the ManagedOrder.state field updating. Polling that
    field is simpler than another event-subscription layer and
    sufficient for a once-a-day cron cadence.

    Timeout behavior: returns the current (non-terminal) state
    after timeout — caller decides whether to cancel. The router
    itself does NOT auto-cancel on timeout (the order stays open
    at the broker's working queue).
    """
    elapsed = 0.0
    while elapsed < timeout_seconds:
        managed = router.get_combo(client_order_id)
        if managed is not None and managed.state in TERMINAL_STATES:
            return managed.state
        await asyncio.sleep(poll_interval_seconds)
        elapsed += poll_interval_seconds
    final = router.get_combo(client_order_id)
    return final.state if final else OrderState.UNKNOWN


def _persist_entry(
    *,
    position_id: str,
    strategy_id: str,
    order: MultiLegOrder,
    snapshot: ChainSnapshot,
    cost: OptionCostModel,
    broker_order_id: str | None,
) -> None:
    """Compute entry-credit + max-loss the same way the runner
    does, then write the open row to the position registry.

    The math here mirrors `runner._open_position_from_order` so
    the live-tracked position carries the same `entry_credit_per_
    share` and `max_loss_per_contract` values that the backtest
    used to validate the strategy. Without this parity the
    `should_close` triggers would fire at different P&L points
    than the validated config predicts.
    """
    op_legs: list[OptionPosition] = []
    persisted_legs: list[PersistedLeg] = []
    for leg_order in order.legs:
        chain_leg = _find_chain_leg(snapshot, leg_order)
        if chain_leg is None:
            raise RuntimeError(
                f"_persist_entry: leg "
                f"{leg_order.expiry}/{leg_order.strike}/{leg_order.side.value} "
                "not present in snapshot — registry write would be "
                "incoherent"
            )
        fill_px = cost.fill_price(
            chain_leg, signed_quantity=leg_order.quantity,
        )
        if fill_px <= 0.0:
            raise RuntimeError(
                f"_persist_entry: cost-model returned non-positive "
                f"fill_px for leg {leg_order.expiry}/{leg_order.strike}"
            )
        op_legs.append(OptionPosition(
            contract_id=OptionPosition.make_contract_id(
                snapshot.underlying,
                leg_order.expiry, leg_order.strike, leg_order.side,
            ),
            underlying=snapshot.underlying,
            expiry=leg_order.expiry,
            strike=leg_order.strike,
            side=leg_order.side,
            multiplier=chain_leg.multiplier,
            quantity=leg_order.quantity,
            entry_price=fill_px,
            entry_ts=snapshot.ts_utc,
        ))
        persisted_legs.append(PersistedLeg(
            expiry=leg_order.expiry.isoformat(),
            strike=leg_order.strike,
            side=leg_order.side.value,
            quantity=leg_order.quantity,
            entry_price=fill_px,
            multiplier=chain_leg.multiplier,
        ))

    cost_to_open_per_share = sum(l.cost_to_open() for l in op_legs)
    entry_credit_per_share = -cost_to_open_per_share
    max_loss = compute_max_loss_per_contract(op_legs, entry_credit_per_share)
    if max_loss <= 0.0:
        # Same refusal the runner does — undefined-risk shape is
        # rejected at registration time. The combo placement already
        # succeeded at the broker; this raise will surface the issue
        # to the caller, who should immediately cancel the broker
        # order and audit.
        raise RuntimeError(
            f"_persist_entry: position {position_id} has non-positive "
            f"max_loss_per_contract={max_loss:.2f}; refusing to "
            "register an undefined-risk shape — cancel the broker "
            "order manually"
        )

    append_open(
        position_id=position_id,
        underlying=snapshot.underlying,
        strategy_class=strategy_id,
        contracts=order.contracts,
        legs=persisted_legs,
        entry_credit_per_share=entry_credit_per_share,
        max_loss_per_contract=max_loss,
        entry_ts=snapshot.ts_utc,
        broker_order_id=broker_order_id,
    )


def _find_chain_leg(
    snapshot: ChainSnapshot, leg_order: LegOrder,
) -> OptionLeg | None:
    for leg in snapshot.for_expiry(leg_order.expiry):
        if leg.strike == leg_order.strike and leg.side == leg_order.side:
            return leg
    return None


def route_decision_sync(
    *,
    decision_entries: list[dict[str, Any]],
    snapshot: ChainSnapshot,
    paper_account: str,
    session_date: datetime,
) -> list[RouteResult]:
    """Sync wrapper around `route_decision` for the CLI surface."""
    return asyncio.run(route_decision(
        decision_entries=decision_entries,
        snapshot=snapshot,
        paper_account=paper_account,
        session_date=session_date,
    ))


# ── Full-session entry-then-close pipeline (V2) ───────────────────


async def _run_full_session_async(
    *,
    decision_entries: list[dict[str, Any]],
    snapshot: ChainSnapshot,
    paper_account: str,
    session_date: datetime,
    rules: Any,
    underlying: str,
) -> dict[str, Any]:
    """Single-connection daily session:

      1. Connect to IBKR paper
      2. Fetch broker positions for `underlying`
      3. Reconcile against local registry (V2)
      4. If reconciliation clean: evaluate should_close, route
         each triggered close, append `close` rows to registry
      5. Route each entry from `decision_entries`, append `open`
         rows to registry on success
      6. Disconnect

    Returns a dict with `reconciliation`, `close_results`, and
    `entry_results` so the CLI surface can render all three.
    """
    from tradegy.live.options_close_loop import (
        evaluate_closes,
        fetch_broker_option_legs,
        reconcile,
        route_close_decisions,
    )
    from tradegy.live.options_position_registry import load_open_positions

    conn = IBKRConnection()
    await conn.connect()
    try:
        accounts = []
        try:
            accounts = conn.ib.managedAccounts()
        except Exception:  # noqa: BLE001
            pass
        _log.info(
            "ibkr connected: host=%s port=%s accounts=%s",
            conn.host, conn.port, accounts,
        )
        if paper_account not in accounts:
            raise RuntimeError(
                f"paper_account={paper_account!r} not in connected "
                f"accounts={accounts!r}; aborting before any order placed"
            )

        router = IbkrOptionsRouter(ib=conn.ib)
        await router.connect()

        # 2-3: reconcile.
        local_positions = load_open_positions()
        broker_legs = fetch_broker_option_legs(
            conn.ib, underlying=underlying,
        )
        reconciliation = reconcile(
            local_positions=local_positions, broker_legs=broker_legs,
        )

        # 4: closes only when reconciliation clean.
        close_results: list[Any] = []
        if not reconciliation.has_divergence and reconciliation.matched:
            closes = evaluate_closes(
                positions=reconciliation.matched,
                snapshot=snapshot, rules=rules,
            )
            close_results = await route_close_decisions(
                closes=closes, snapshot=snapshot, router=router,
                session_date=session_date,
            )

        # 5: entries — place + await fill confirmation.
        cost = OptionCostModel()
        entry_results: list[RouteResult] = []
        for intent_seq, entry in enumerate(decision_entries):
            order = _entry_dict_to_order(entry)
            coid = make_client_order_id(
                strategy_id=entry["strategy_id"],
                session_date=session_date,
                intent_seq=intent_seq,
                role=OrderRole.ENTRY,
            )
            try:
                managed = await router.place_combo(
                    order=order, snapshot=snapshot, client_order_id=coid,
                )
                # Wait for terminal state. If FILLED → write
                # registry. If WORKING after timeout → cancel +
                # warn (no registry side effect, next session
                # will re-attempt). If REJECTED → record error.
                final_state = await await_terminal_state(
                    router=router, client_order_id=coid,
                )
                if final_state == OrderState.FILLED:
                    _persist_entry(
                        position_id=coid,
                        strategy_id=entry["strategy_id"],
                        order=order, snapshot=snapshot, cost=cost,
                        broker_order_id=managed.broker_order_id,
                    )
                    entry_results.append(RouteResult(
                        strategy_id=entry["strategy_id"],
                        client_order_id=coid,
                        accepted=True,
                        broker_order_id=managed.broker_order_id,
                        final_state=final_state.value,
                    ))
                elif final_state in TERMINAL_STATES:
                    # REJECTED, CANCELLED, EXPIRED — surface as
                    # not-accepted; no registry write.
                    entry_results.append(RouteResult(
                        strategy_id=entry["strategy_id"],
                        client_order_id=coid,
                        accepted=False,
                        broker_order_id=managed.broker_order_id,
                        final_state=final_state.value,
                        error=f"order ended in {final_state.value} not FILLED",
                    ))
                else:
                    # Non-terminal after timeout (WORKING / PARTIAL).
                    # Cancel so the limit doesn't sit unfilled
                    # forever; surface as not-accepted.
                    try:
                        await router.cancel_combo(coid)
                    except Exception:  # noqa: BLE001
                        _log.exception(
                            "cancel after timeout failed for %s", coid,
                        )
                    entry_results.append(RouteResult(
                        strategy_id=entry["strategy_id"],
                        client_order_id=coid,
                        accepted=False,
                        broker_order_id=managed.broker_order_id,
                        final_state=final_state.value,
                        error=(
                            f"order in {final_state.value} after fill-"
                            "wait timeout — cancelled"
                        ),
                    ))
            except Exception as exc:  # noqa: BLE001
                _log.exception("place_combo failed for %s", entry["strategy_id"])
                entry_results.append(RouteResult(
                    strategy_id=entry["strategy_id"],
                    client_order_id=coid,
                    accepted=False,
                    error=f"{type(exc).__name__}: {exc}",
                ))

        return {
            "reconciliation": reconciliation,
            "close_results": close_results,
            "entry_results": entry_results,
        }
    finally:
        await conn.disconnect()


def run_full_session(
    *,
    decision_entries: list[dict[str, Any]],
    snapshot: ChainSnapshot,
    paper_account: str,
    session_date: datetime,
    rules: Any,
    underlying: str,
) -> dict[str, Any]:
    """Sync wrapper for the CLI surface."""
    return asyncio.run(_run_full_session_async(
        decision_entries=decision_entries,
        snapshot=snapshot,
        paper_account=paper_account,
        session_date=session_date,
        rules=rules,
        underlying=underlying,
    ))
