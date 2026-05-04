"""Backfill the position registry from operator-supplied position
descriptors.

When the V2 reconciliation pass surfaces `broker_only` divergence
(positions held at the broker that the registry doesn't know about
— e.g. the operator opened them via TWS, or held them before
installing this system), the V2 close loop CAN'T MANAGE them.
They sit divergent and pause the close loop indefinitely.

This module loads operator-supplied YAML/JSON describing each
pre-existing position with enough detail to reconstruct a proper
MultiLegPosition: strategy_class, contracts, every leg with its
strike/expiry/side/quantity/entry_price/multiplier, plus the
entry_ts. It then computes entry_credit_per_share +
max_loss_per_contract using the SAME math as the
runner/router (so should_close triggers fire at the same P&L
points the validated config predicts) and appends `open` rows
to the registry.

After backfill, the next live-options session's reconciliation
will match the broker positions and the close loop manages them
normally.

JSON schema (one position per top-level entry):

    [
      {
        "position_id": "pre_existing_pcs_apr17_001",
        "underlying": "SPY",
        "strategy_class": "iv_gated_max0.25_put_credit_spread_45dte_d30",
        "contracts": 1,
        "entry_ts": "2026-04-15T20:46:00+00:00",
        "broker_order_id": "ibkr_98765",  // null if unknown
        "legs": [
          {
            "expiry": "2026-05-15",
            "strike": 480.0,
            "side": "put",
            "quantity": -1,
            "entry_price": 2.50,
            "multiplier": 100
          },
          {
            "expiry": "2026-05-15",
            "strike": 475.0,
            "side": "put",
            "quantity": +1,
            "entry_price": 1.20,
            "multiplier": 100
          }
        ]
      }
    ]

The operator must source `entry_price` per leg from their IBKR
statement (or memory, if recent) — there's no way to recover it
from current chain quotes. position_id should be unique and
ideally encode "this is a backfill" (e.g. prefix with `backfill_`)
so audit trails distinguish backfilled vs system-routed entries.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from tradegy.live.options_position_registry import (
    PersistedLeg,
    append_open,
    load_open_positions,
)
from tradegy.options.chain import OptionSide
from tradegy.options.positions import (
    OptionPosition,
    compute_max_loss_per_contract,
)


@dataclass
class BackfillResult:
    """Per-position backfill outcome."""

    position_id: str
    appended: bool
    skipped_reason: str | None = None
    computed_entry_credit_per_share: float = 0.0
    computed_max_loss_per_contract: float = 0.0


def backfill_from_file(
    *,
    json_path: Path,
    registry_root: Path | None = None,
) -> list[BackfillResult]:
    """Load `json_path`, validate each position, append to registry.

    Already-registered position_ids are skipped (idempotent re-run
    is safe). Validation failures (missing field, bad shape,
    undefined-risk math) are reported per-position; the call does
    not abort on first failure.
    """
    raw = json.loads(json_path.read_text())
    if not isinstance(raw, list):
        raise ValueError(
            f"backfill JSON must be a top-level list of positions; "
            f"got {type(raw).__name__}"
        )
    existing_ids = {p.position_id for p in load_open_positions(root=registry_root)}
    results: list[BackfillResult] = []
    for i, entry in enumerate(raw):
        pid = entry.get("position_id", f"<entry {i}>")
        if pid in existing_ids:
            results.append(BackfillResult(
                position_id=pid, appended=False,
                skipped_reason="already_in_registry",
            ))
            continue
        try:
            result = _backfill_one(entry, registry_root=registry_root)
            results.append(result)
        except Exception as exc:  # noqa: BLE001
            results.append(BackfillResult(
                position_id=pid, appended=False,
                skipped_reason=f"{type(exc).__name__}: {exc}",
            ))
    return results


def _backfill_one(
    entry: dict[str, Any],
    *,
    registry_root: Path | None = None,
) -> BackfillResult:
    """Validate one position descriptor and append if clean."""
    required = {
        "position_id", "underlying", "strategy_class", "contracts",
        "entry_ts", "legs",
    }
    missing = required - set(entry.keys())
    if missing:
        raise ValueError(f"missing required fields: {sorted(missing)}")
    if not isinstance(entry["legs"], list) or len(entry["legs"]) < 2:
        raise ValueError("legs must be a list of ≥2 legs (multi-leg position)")

    persisted_legs: list[PersistedLeg] = []
    op_legs: list[OptionPosition] = []
    underlying = entry["underlying"]
    entry_ts = _parse_iso_dt(entry["entry_ts"])
    for j, leg_dict in enumerate(entry["legs"]):
        for f in ("expiry", "strike", "side", "quantity", "entry_price",
                  "multiplier"):
            if f not in leg_dict:
                raise ValueError(f"leg {j} missing field {f!r}")
        expiry = date.fromisoformat(leg_dict["expiry"])
        side = OptionSide(leg_dict["side"])
        strike = float(leg_dict["strike"])
        qty = int(leg_dict["quantity"])
        entry_price = float(leg_dict["entry_price"])
        multiplier = int(leg_dict["multiplier"])
        if qty == 0:
            raise ValueError(f"leg {j} has quantity=0 (no-op leg)")
        if entry_price <= 0.0:
            raise ValueError(
                f"leg {j} has non-positive entry_price={entry_price}; "
                "ORATS-style mid prices should be > 0"
            )
        persisted_legs.append(PersistedLeg(
            expiry=expiry.isoformat(),
            strike=strike, side=side.value, quantity=qty,
            entry_price=entry_price, multiplier=multiplier,
        ))
        op_legs.append(OptionPosition(
            contract_id=OptionPosition.make_contract_id(
                underlying, expiry, strike, side,
            ),
            underlying=underlying, expiry=expiry, strike=strike,
            side=side, multiplier=multiplier, quantity=qty,
            entry_price=entry_price, entry_ts=entry_ts,
        ))

    cost_to_open_per_share = sum(l.cost_to_open() for l in op_legs)
    entry_credit_per_share = -cost_to_open_per_share
    max_loss = compute_max_loss_per_contract(op_legs, entry_credit_per_share)
    if max_loss <= 0.0:
        raise ValueError(
            f"max_loss_per_contract={max_loss} ≤ 0 — position has "
            "undefined risk per the leg structure + entry credit; "
            "refusing to register an undefined-risk shape"
        )

    append_open(
        position_id=entry["position_id"],
        underlying=underlying,
        strategy_class=entry["strategy_class"],
        contracts=int(entry["contracts"]),
        legs=persisted_legs,
        entry_credit_per_share=entry_credit_per_share,
        max_loss_per_contract=max_loss,
        entry_ts=entry_ts,
        broker_order_id=entry.get("broker_order_id"),
        root=registry_root,
    )
    return BackfillResult(
        position_id=entry["position_id"],
        appended=True,
        computed_entry_credit_per_share=entry_credit_per_share,
        computed_max_loss_per_contract=max_loss,
    )


def _parse_iso_dt(s: str) -> datetime:
    from datetime import timezone
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
