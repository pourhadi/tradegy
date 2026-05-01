"""EvidencePacket dataclass + read/write helpers.

A packet is the signed atomic record of a harness run (backtest /
walk-forward / CPCV). Schema is intentionally narrow — the things
governance needs to know to promote a strategy:

  * spec identity (id, version, content sha)
  * harness identity (package version)
  * cost model used
  * coverage window
  * run type and aggregate stats
  * signature over the canonical-JSON payload

The full trade ledger is NOT included by default — it would balloon the
packet for long backtests. Instead the packet records `trade_count` and
`trades_sha256` (a hash of the canonical-JSON trade list); auditors
can reconstruct the trades by re-running the spec at the same harness
version and verifying the hash.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from tradegy import __version__ as tradegy_version
from tradegy import config
from tradegy.evidence.signing import canonical_json, sign, verify


RunType = Literal["backtest", "walk_forward", "cpcv"]
PACKET_SCHEMA_VERSION = "1.0"


@dataclass
class EvidencePacket:
    """Signed evidence document. The `signature` field covers everything
    above it via `canonical_json()`. Verifying re-canonicalizes the
    same fields and recomputes the signature.
    """

    schema_version: str
    spec_id: str
    spec_version: str
    spec_sha256: str
    harness_version: str
    run_type: RunType
    cost_model: dict[str, float]
    coverage_start: str  # ISO 8601
    coverage_end: str  # ISO 8601
    generated_at: str  # ISO 8601
    payload: dict[str, Any]
    signature: dict[str, str] = field(default_factory=dict)


def _spec_sha256(spec_path: Path | None) -> str:
    if spec_path is None or not spec_path.exists():
        return ""
    return hashlib.sha256(spec_path.read_bytes()).hexdigest()


def _payload_to_signable(packet: EvidencePacket) -> str:
    """Canonical JSON of all fields EXCEPT the signature itself."""
    d = asdict(packet)
    d.pop("signature", None)
    return canonical_json(d)


def build_packet(
    *,
    spec_id: str,
    spec_version: str,
    spec_path: Path | None,
    run_type: RunType,
    cost_model: dict[str, float],
    coverage_start: datetime,
    coverage_end: datetime,
    payload: dict[str, Any],
) -> EvidencePacket:
    """Build and sign a packet. The caller assembles `payload`; this
    function adds the canonical envelope + signature.
    """
    packet = EvidencePacket(
        schema_version=PACKET_SCHEMA_VERSION,
        spec_id=spec_id,
        spec_version=spec_version,
        spec_sha256=_spec_sha256(spec_path),
        harness_version=tradegy_version,
        run_type=run_type,
        cost_model=dict(cost_model),
        coverage_start=coverage_start.isoformat(),
        coverage_end=coverage_end.isoformat(),
        generated_at=datetime.now(tz=timezone.utc).isoformat(),
        payload=payload,
    )
    packet.signature = sign(_payload_to_signable(packet))
    return packet


def write_packet(packet: EvidencePacket, *, out_dir: Path | None = None) -> Path:
    """Persist packet under `data/evidence/<spec_id>_<run_type>_<ts>.json`."""
    base = out_dir or config.evidence_dir()
    base.mkdir(parents=True, exist_ok=True)
    ts = packet.generated_at.replace(":", "").replace("-", "").split(".")[0]
    name = f"{packet.spec_id}__{packet.run_type}__{ts}.json"
    out_path = base / name
    out_path.write_text(canonical_json(asdict(packet)) + "\n")
    return out_path


def read_packet(path: Path) -> EvidencePacket:
    import json
    raw = json.loads(path.read_text())
    return EvidencePacket(**raw)


def verify_packet(packet: EvidencePacket) -> tuple[bool, str]:
    """Verify the packet's signature against its current contents.
    Returns (passed, message).
    """
    payload_json = _payload_to_signable(packet)
    return verify(payload_json, packet.signature)
