"""Append-only transition log.

Per `11_execution_layer_spec.md:97-99`. The log is the system of record
for every order's lifecycle history. Replaying the log reconstructs
the (state, filled_quantity, broker_order_id) for any order at any
point in time.

The MVP uses a JSONL file on disk: one transition per line, in the
order written. This is good enough for single-process use; multi-
process or distributed operation needs a real append-only store
(SQLite WAL or a log service), but that is out of scope for v1.

Replay is offered for two purposes:
  1. Restart recovery — rebuild the in-memory order map after a crash.
  2. Audit / governance — re-derive what the system "knew" at any
     historical instant for a `13_governance_process.md` review.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from tradegy.execution.lifecycle import (
    OrderState,
    TransitionRecord,
    TransitionSource,
)


def _record_to_dict(rec: TransitionRecord) -> dict:
    d = asdict(rec)
    d["from_state"] = rec.from_state.value
    d["to_state"] = rec.to_state.value
    d["source"] = rec.source.value
    d["ts_utc"] = rec.ts_utc.astimezone(timezone.utc).isoformat()
    return d


def _record_from_dict(d: dict) -> TransitionRecord:
    return TransitionRecord(
        order_id=d["order_id"],
        from_state=OrderState(d["from_state"]),
        to_state=OrderState(d["to_state"]),
        ts_utc=datetime.fromisoformat(d["ts_utc"]),
        source=TransitionSource(d["source"]),
        reason=d.get("reason", ""),
        detail=dict(d.get("detail", {})),
    )


class TransitionLog:
    """Append-only JSONL transition log.

    Open the log with a path; `append` writes one record per call;
    `read_all` iterates every record from the start of the file.
    The file is opened in append-binary mode on each write so concurrent
    processes don't truncate; ordering across processes is best-effort
    and the spec calls out that v1 is single-process only (see
    `11_execution_layer_spec.md` ¶ "MVP uses a JSONL file").
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def append(self, record: TransitionRecord) -> None:
        line = json.dumps(_record_to_dict(record), separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def read_all(self) -> Iterator[TransitionRecord]:
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                yield _record_from_dict(json.loads(line))

    def replay_until(
        self, ts_utc: datetime
    ) -> Iterator[TransitionRecord]:
        """Yield every record with `ts_utc <= ts_utc`, in order. Used
        for governance reviews to reconstruct what the system 'knew'
        at a historical instant.
        """
        for rec in self.read_all():
            if rec.ts_utc <= ts_utc:
                yield rec
