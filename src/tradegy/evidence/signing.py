"""Canonical-JSON serialization + HMAC/SHA256 signing primitives.

Two signing modes:

  - **HMAC-SHA256** (unforgeable, recommended): set `TRADEGY_EVIDENCE_KEY`
    in the environment. The shared secret is required to forge or verify
    a signature. This is the mode required by the governance workflow
    in `13_governance_process.md` for promotion to `live` tier.

  - **SHA256** (tamper-evident only): if `TRADEGY_EVIDENCE_KEY` is unset,
    the harness signs with a plain SHA256 digest. Any post-hoc edit of
    the payload invalidates the digest, but the digest itself is not
    unforgeable — anyone can recompute it from a tampered payload. The
    packet records `algorithm: "SHA256"` plus a `warning` field so
    downstream tools (`tradegy validate-evidence`) can reject this for
    governance-grade decisions.

The canonical JSON serialization uses sorted keys, comma+colon
separators, and ISO-formatted datetimes so round-trip equality holds
across Python releases and OS locales.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import is_dataclass, asdict
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal

_ENV_VAR = "TRADEGY_EVIDENCE_KEY"
_HMAC_ALGO: Literal["HMAC-SHA256"] = "HMAC-SHA256"
_HASH_ALGO: Literal["SHA256"] = "SHA256"


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, Enum):
        return o.value
    if is_dataclass(o):
        return asdict(o)
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    raise TypeError(
        f"canonical_json: type {type(o).__name__} is not JSON-serializable"
    )


def canonical_json(obj: Any) -> str:
    """Stable JSON: sorted keys, no whitespace, deterministic across runs."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
        ensure_ascii=False,
    )


def signing_mode() -> Literal["HMAC-SHA256", "SHA256"]:
    """Return the active signing mode based on environment."""
    if os.environ.get(_ENV_VAR):
        return _HMAC_ALGO
    return _HASH_ALGO


def sign(payload_json: str) -> dict[str, str]:
    """Sign the canonical-JSON payload string. Returns a dict with
    `algorithm` and `signature`, plus a `warning` if running in
    non-unforgeable SHA256 mode.
    """
    key_str = os.environ.get(_ENV_VAR)
    if key_str:
        sig = hmac.new(
            key_str.encode("utf-8"),
            payload_json.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {"algorithm": _HMAC_ALGO, "signature": sig}
    sig = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    return {
        "algorithm": _HASH_ALGO,
        "signature": sig,
        "warning": (
            f"{_ENV_VAR} unset — signature is tamper-evident only, not "
            "unforgeable. Governance promotion to `live` tier requires HMAC."
        ),
    }


def verify(payload_json: str, signature_obj: dict[str, str]) -> tuple[bool, str]:
    """Recompute the signature and constant-time-compare. Returns
    (passed, message).
    """
    algo = signature_obj.get("algorithm")
    expected = signature_obj.get("signature")
    if not isinstance(expected, str) or not algo:
        return False, "signature object missing 'algorithm' or 'signature'"
    if algo == _HMAC_ALGO:
        key_str = os.environ.get(_ENV_VAR)
        if not key_str:
            return False, (
                f"packet signed with HMAC but {_ENV_VAR} not set; "
                "cannot verify"
            )
        actual = hmac.new(
            key_str.encode("utf-8"),
            payload_json.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    elif algo == _HASH_ALGO:
        actual = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    else:
        return False, f"unknown algorithm {algo!r}"
    if hmac.compare_digest(expected, actual):
        return True, f"signature OK ({algo})"
    return False, f"signature mismatch: payload differs from when signed"
