"""Signed-evidence packets for harness outputs.

Per `05_backtest_harness.md` design principle 5: the harness signs its
output so humans cannot edit stats after the fact, and any modification
invalidates the signature. The signed evidence is the input to the
governance promotion workflow defined in `13_governance_process.md`.
"""
from tradegy.evidence.packet import (
    EvidencePacket,
    build_packet,
    read_packet,
    write_packet,
)
from tradegy.evidence.signing import (
    canonical_json,
    sign,
    signing_mode,
    verify,
)

__all__ = [
    "EvidencePacket",
    "build_packet",
    "canonical_json",
    "read_packet",
    "sign",
    "signing_mode",
    "verify",
    "write_packet",
]
