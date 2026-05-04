"""Local registry of routed options positions for the V2
reconciliation loop.

The backtest's `MultiLegPosition` is built up snapshot-by-snapshot
inside the runner. Live trading needs a *persistent* analogue: when
we route an entry to IBKR, we record the structural details (legs,
entry credit, max loss, contracts, ts) so that on subsequent
sessions we can:

  1. Reconstruct `MultiLegPosition` objects from disk.
  2. Mark-to-market them against the current chain snapshot.
  3. Run `should_close` against current rules.
  4. Route closing combos for triggered positions.
  5. Reconcile our local view against the broker's actual
     positions; alert on divergence.

JSONL format — one line per position-state-change:

  type=open: written when route_decision successfully places an
    entry. Contains every leg (strike/expiry/side/qty), entry
    credit per share, max loss per contract, contracts, entry ts,
    client_order_id, broker_order_id.
  type=close: written when route_decision successfully places a
    close OR when reconciliation detects a broker-side close
    we didn't initiate (assignment, manual TWS close, etc.).
    Contains position_id + closed_ts + closed_reason +
    closed_pnl_per_share.

Open positions are computed by reading the JSONL: any position_id
with an `open` event but no later `close` event is currently open.
This append-only design avoids race conditions and gives us a
complete audit trail.

Per `14_options_volatility_selling.md` Phase E. The registry
is the local source of truth; reconciliation against IBKR is a
separate concern in `options_routing.fetch_broker_positions`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from tradegy import config
from tradegy.options.chain import OptionSide
from tradegy.options.positions import MultiLegPosition, OptionPosition


_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PersistedLeg:
    """Serializable leg of a registered position."""

    expiry: str           # ISO date — stays a string in JSON
    strike: float
    side: str             # "call" | "put"
    quantity: int         # signed: + long, - short
    entry_price: float    # per-share fill recorded at routing
    multiplier: int


@dataclass(frozen=True)
class PersistedOpen:
    """JSONL row written when a routed entry succeeds."""

    type: str = field(default="open", init=False)
    position_id: str = ""        # = client_order_id (idempotent across re-runs)
    underlying: str = ""
    strategy_class: str = ""     # = strategy_id (e.g. iv_gated_max0.25_pcs_45dte_d30)
    contracts: int = 0
    legs: tuple[PersistedLeg, ...] = ()
    entry_credit_per_share: float = 0.0
    max_loss_per_contract: float = 0.0
    entry_ts: str = ""           # ISO datetime
    broker_order_id: str | None = None


@dataclass(frozen=True)
class PersistedClose:
    """JSONL row written when a position closes (initiated or
    detected). Closed-pnl-per-share is filled when we have a fill
    confirmation; for broker-detected closes the operator's
    fill price is the source of truth.
    """

    type: str = field(default="close", init=False)
    position_id: str = ""
    closed_ts: str = ""
    closed_reason: str = ""
    closed_pnl_per_share: float | None = None
    broker_close_order_id: str | None = None


def registry_path(*, root: Path | None = None) -> Path:
    """Default path for the JSONL registry. Override with `root` in
    tests."""
    base = root or (config.repo_root() / "data" / "live_options")
    base.mkdir(parents=True, exist_ok=True)
    return base / "positions.jsonl"


def append_open(
    *,
    position_id: str,
    underlying: str,
    strategy_class: str,
    contracts: int,
    legs: list[PersistedLeg],
    entry_credit_per_share: float,
    max_loss_per_contract: float,
    entry_ts: datetime,
    broker_order_id: str | None,
    root: Path | None = None,
) -> None:
    """Write an `open` row. Caller is responsible for ensuring
    `position_id` is unique — the orchestrator uses the broker
    `client_order_id` which is idempotent.
    """
    row = PersistedOpen(
        position_id=position_id,
        underlying=underlying,
        strategy_class=strategy_class,
        contracts=contracts,
        legs=tuple(legs),
        entry_credit_per_share=entry_credit_per_share,
        max_loss_per_contract=max_loss_per_contract,
        entry_ts=entry_ts.isoformat(),
        broker_order_id=broker_order_id,
    )
    _append_row(row, root=root)


def append_close(
    *,
    position_id: str,
    closed_ts: datetime,
    closed_reason: str,
    closed_pnl_per_share: float | None = None,
    broker_close_order_id: str | None = None,
    root: Path | None = None,
) -> None:
    """Write a `close` row. The position must have an earlier
    matching `open` row for the close to make sense; load_open_
    positions() filters out the position once it sees the close.
    """
    row = PersistedClose(
        position_id=position_id,
        closed_ts=closed_ts.isoformat(),
        closed_reason=closed_reason,
        closed_pnl_per_share=closed_pnl_per_share,
        broker_close_order_id=broker_close_order_id,
    )
    _append_row(row, root=root)


def load_open_positions(
    *, root: Path | None = None,
) -> list[MultiLegPosition]:
    """Read the registry and return MultiLegPosition objects for
    every position with an `open` event but no later `close`.

    Intended consumer: the close-trigger evaluator that runs
    `should_close` against today's chain snapshot.
    """
    path = registry_path(root=root)
    if not path.exists():
        return []
    opens: dict[str, PersistedOpen] = {}
    closed_ids: set[str] = set()
    with path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row["type"] == "open":
                opens[row["position_id"]] = _parse_open(row)
            elif row["type"] == "close":
                closed_ids.add(row["position_id"])
            else:
                raise ValueError(
                    f"unknown registry row type {row['type']!r} "
                    f"in {path}"
                )
    out: list[MultiLegPosition] = []
    for pid, op in opens.items():
        if pid in closed_ids:
            continue
        out.append(_to_multi_leg_position(op))
    return out


# ── internals ─────────────────────────────────────────────────────


def _append_row(
    row: PersistedOpen | PersistedClose, *, root: Path | None = None,
) -> None:
    path = registry_path(root=root)
    payload = asdict(row)
    # asdict drops the type sentinel because it's a default field.
    # Re-attach so the reader can dispatch on it.
    payload["type"] = row.type
    with path.open("a") as f:
        f.write(json.dumps(payload, default=str) + "\n")


def _parse_open(row: dict[str, Any]) -> PersistedOpen:
    legs = tuple(
        PersistedLeg(
            expiry=leg["expiry"],
            strike=float(leg["strike"]),
            side=leg["side"],
            quantity=int(leg["quantity"]),
            entry_price=float(leg["entry_price"]),
            multiplier=int(leg["multiplier"]),
        )
        for leg in row["legs"]
    )
    return PersistedOpen(
        position_id=row["position_id"],
        underlying=row["underlying"],
        strategy_class=row["strategy_class"],
        contracts=int(row["contracts"]),
        legs=legs,
        entry_credit_per_share=float(row["entry_credit_per_share"]),
        max_loss_per_contract=float(row["max_loss_per_contract"]),
        entry_ts=row["entry_ts"],
        broker_order_id=row.get("broker_order_id"),
    )


def _to_multi_leg_position(op: PersistedOpen) -> MultiLegPosition:
    """Re-hydrate a PersistedOpen into a MultiLegPosition.

    Each persisted leg becomes an OptionPosition with the recorded
    entry_price and entry_ts (we store the routing-time fill price
    in PersistedLeg.entry_price so mark-to-market against today's
    chain produces correct P&L).
    """
    entry_ts_dt = _parse_iso_dt(op.entry_ts)
    legs = tuple(
        OptionPosition(
            contract_id=OptionPosition.make_contract_id(
                op.underlying,
                date.fromisoformat(leg.expiry),
                leg.strike,
                OptionSide(leg.side),
            ),
            underlying=op.underlying,
            expiry=date.fromisoformat(leg.expiry),
            strike=leg.strike,
            side=OptionSide(leg.side),
            multiplier=leg.multiplier,
            quantity=leg.quantity,
            entry_price=leg.entry_price,
            entry_ts=entry_ts_dt,
        )
        for leg in op.legs
    )
    return MultiLegPosition(
        position_id=op.position_id,
        strategy_class=op.strategy_class,
        contracts=op.contracts,
        legs=legs,
        entry_ts=entry_ts_dt,
        entry_credit_per_share=op.entry_credit_per_share,
        max_loss_per_contract=op.max_loss_per_contract,
    )


def _parse_iso_dt(s: str) -> datetime:
    """Parse ISO-8601 datetime; assume UTC if naive."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
