"""Tests for the install-verification doctor.

The pure-data checks (env, wrapper-syntax, plist-syntax, registry,
chain-freshness) are testable with monkeypatch. The IBKR + ORATS
checks require live infrastructure and are not unit-tested here —
they're integration-ish, exercised only when the operator runs
the doctor.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tradegy.live.options_doctor import (
    CheckResult,
    _check_env,
    _check_plist,
    _check_registry_sanity,
    _check_wrapper,
    run_all_checks,
)


def test_check_env_pass(monkeypatch):
    monkeypatch.setenv("ORATS_API_KEY", "test_key")
    monkeypatch.setenv("IBKR_PAPER_ACCOUNT", "DU99999")
    r = _check_env()
    assert r.status == "pass"
    assert "DU99999" in r.detail


def test_check_env_fail_when_missing(monkeypatch):
    monkeypatch.delenv("ORATS_API_KEY", raising=False)
    monkeypatch.delenv("IBKR_PAPER_ACCOUNT", raising=False)
    r = _check_env()
    assert r.status == "fail"
    assert "ORATS_API_KEY" in r.message
    assert "IBKR_PAPER_ACCOUNT" in r.message


def test_check_wrapper_pass():
    """The repo's own wrapper script should pass — it's executable
    + bash-syntax-clean by construction.
    """
    r = _check_wrapper()
    assert r.status == "pass"


def test_check_plist_warning_when_not_loaded():
    """The repo's plist passes plutil -lint but isn't loaded in
    the test environment's launchctl, so we expect WARNING.
    """
    r = _check_plist()
    # Status is either pass (if loaded — unlikely in test env) or
    # warning (not loaded). Either is acceptable; check it's not FAIL.
    assert r.status in {"pass", "warning"}


def test_check_registry_sanity_returns_count():
    """Registry sanity always passes (empty registry is fine);
    message includes a count.
    """
    r = _check_registry_sanity()
    assert r.status == "pass"
    assert "open position" in r.message


def test_run_all_checks_returns_one_result_per_check():
    """run_all_checks returns the full set; one CheckResult per
    check function. With skip_ibkr=True the IBKR checks become
    "skip" status (not omitted from the list).
    """
    results = run_all_checks(skip_ibkr=True)
    assert len(results) >= 7
    names = [r.name for r in results]
    # The major checks are present.
    assert "ENV" in names
    assert "WRAPPER" in names
    assert "PLIST" in names
    assert "REGISTRY" in names


def test_check_individual_isolation_one_failure_doesnt_stop_others(monkeypatch):
    """If env vars are missing, the env check fails — but every
    other check still runs and returns a result.
    """
    monkeypatch.delenv("ORATS_API_KEY", raising=False)
    monkeypatch.delenv("IBKR_PAPER_ACCOUNT", raising=False)
    results = run_all_checks(skip_ibkr=True)
    env_result = next(r for r in results if r.name == "ENV")
    assert env_result.status == "fail"
    # Other checks ran — at least one besides ENV is present.
    non_env = [r for r in results if r.name != "ENV"]
    assert len(non_env) >= 6
