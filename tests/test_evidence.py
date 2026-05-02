"""Evidence-packet signing + verification tests.

Per `05_backtest_harness.md` design principle 5 ("signed outputs"). The
evidence module backs the governance promotion gate in
`13_governance_process.md`; security-sensitive enough to warrant focused
unit tests.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tradegy.evidence.packet import (
    EvidencePacket,
    build_packet,
    read_packet,
    verify_packet,
    write_packet,
)
from tradegy.evidence.signing import canonical_json, sign, signing_mode, verify


_KEY_ENV = "TRADEGY_EVIDENCE_KEY"


@pytest.fixture(autouse=True)
def _clear_key(monkeypatch):
    """Each test starts with no signing key. Tests that want HMAC set
    it explicitly via monkeypatch.setenv.
    """
    monkeypatch.delenv(_KEY_ENV, raising=False)


def test_canonical_json_is_stable():
    a = canonical_json({"b": 2, "a": [3, 1, 2], "c": {"y": 1, "x": 2}})
    b = canonical_json({"a": [3, 1, 2], "c": {"x": 2, "y": 1}, "b": 2})
    assert a == b
    assert a == '{"a":[3,1,2],"b":2,"c":{"x":2,"y":1}}'


def test_canonical_json_handles_datetime():
    ts = datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)
    out = canonical_json({"t": ts})
    assert "2026-05-01T12:30:00+00:00" in out


def test_signing_defaults_to_sha256_with_warning():
    sig = sign("hello")
    assert sig["algorithm"] == "SHA256"
    assert "warning" in sig
    assert signing_mode() == "SHA256"


def test_signing_uses_hmac_when_key_set(monkeypatch):
    monkeypatch.setenv(_KEY_ENV, "secret")
    sig = sign("hello")
    assert sig["algorithm"] == "HMAC-SHA256"
    assert "warning" not in sig
    assert signing_mode() == "HMAC-SHA256"


def test_sha256_round_trip():
    payload = canonical_json({"x": 1, "y": [2, 3]})
    sig = sign(payload)
    passed, _msg = verify(payload, sig)
    assert passed


def test_sha256_detects_tamper():
    payload = canonical_json({"x": 1})
    sig = sign(payload)
    tampered = canonical_json({"x": 99})
    passed, msg = verify(tampered, sig)
    assert not passed
    assert "mismatch" in msg


def test_hmac_round_trip(monkeypatch):
    monkeypatch.setenv(_KEY_ENV, "secret-key")
    payload = canonical_json({"x": 1})
    sig = sign(payload)
    passed, _ = verify(payload, sig)
    assert passed


def test_hmac_rejects_wrong_key(monkeypatch):
    monkeypatch.setenv(_KEY_ENV, "key-A")
    payload = canonical_json({"x": 1})
    sig = sign(payload)

    monkeypatch.setenv(_KEY_ENV, "key-B")
    passed, msg = verify(payload, sig)
    assert not passed


def test_hmac_packet_cannot_verify_without_key(monkeypatch):
    monkeypatch.setenv(_KEY_ENV, "k")
    sig = sign(canonical_json({"x": 1}))

    monkeypatch.delenv(_KEY_ENV)
    passed, msg = verify(canonical_json({"x": 1}), sig)
    assert not passed
    assert "TRADEGY_EVIDENCE_KEY" in msg


def test_build_and_verify_packet(tmp_path: Path):
    packet = build_packet(
        spec_id="demo",
        spec_version="0.1.0",
        spec_path=None,
        run_type="backtest",
        cost_model={"tick_size": 0.25, "slippage": 0.5},
        coverage_start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        coverage_end=datetime(2024, 12, 31, tzinfo=timezone.utc),
        payload={"sharpe": 1.23, "total_trades": 100},
    )
    passed, _ = verify_packet(packet)
    assert passed


def test_packet_round_trip_through_disk(tmp_path: Path):
    packet = build_packet(
        spec_id="demo",
        spec_version="0.1.0",
        spec_path=None,
        run_type="walk_forward",
        cost_model={"tick_size": 0.25},
        coverage_start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        coverage_end=datetime(2024, 12, 31, tzinfo=timezone.utc),
        payload={"avg_oos_sharpe": 0.42},
    )
    out_path = write_packet(packet, out_dir=tmp_path)
    loaded = read_packet(out_path)
    passed, _ = verify_packet(loaded)
    assert passed
    assert loaded.spec_id == "demo"
    assert loaded.payload == {"avg_oos_sharpe": 0.42}


def test_packet_detects_tamper_on_disk(tmp_path: Path):
    import json

    packet = build_packet(
        spec_id="demo",
        spec_version="0.1.0",
        spec_path=None,
        run_type="cpcv",
        cost_model={},
        coverage_start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        coverage_end=datetime(2024, 12, 31, tzinfo=timezone.utc),
        payload={"median_sharpe": 0.85},
    )
    out_path = write_packet(packet, out_dir=tmp_path)

    raw = json.loads(out_path.read_text())
    raw["payload"]["median_sharpe"] = 99.9
    out_path.write_text(json.dumps(raw))

    loaded = read_packet(out_path)
    passed, msg = verify_packet(loaded)
    assert not passed
    assert "mismatch" in msg


def test_spec_sha256_is_recorded_when_path_given(tmp_path: Path):
    spec_yaml = tmp_path / "spec.yaml"
    spec_yaml.write_text("metadata:\n  id: demo\n")
    packet = build_packet(
        spec_id="demo",
        spec_version="0.1.0",
        spec_path=spec_yaml,
        run_type="backtest",
        cost_model={},
        coverage_start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        coverage_end=datetime(2024, 12, 31, tzinfo=timezone.utc),
        payload={},
    )
    assert packet.spec_sha256 != ""
    assert len(packet.spec_sha256) == 64  # sha256 hex
