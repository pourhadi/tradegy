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
from tradegy.live.ibkr import IBKRConnection
from tradegy.options.chain import ChainSnapshot, OptionLeg, OptionSide
from tradegy.options.chain_io import iter_chain_snapshots
from tradegy.options.positions import LegOrder, MultiLegOrder


_log = logging.getLogger(__name__)


@dataclass
class RouteResult:
    """Outcome of routing one entry candidate to IBKR."""

    strategy_id: str
    client_order_id: str
    accepted: bool
    broker_order_id: str | None = None
    error: str | None = None
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
