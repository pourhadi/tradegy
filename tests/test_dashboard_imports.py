"""Smoke test for the Streamlit dashboard module.

The dashboard renders interactively in a browser; we don't drive
that here. But importing the module must succeed (no syntax errors,
no missing deps), and the data-loader functions must be callable
in isolation since they're cached and parameterless.

The data loaders use Streamlit's @st.cache_data decorator which
requires running inside a Streamlit runtime to populate the cache;
calling them outside the runtime emits a warning but still
executes the underlying function. We assert the underlying call
returns the right shape.
"""
from __future__ import annotations

import pytest


def test_dashboard_module_imports():
    """Module imports cleanly — no syntax errors, no missing deps."""
    from tradegy.dashboard import app  # noqa: F401


def test_dashboard_main_callable():
    """`main()` is a callable function. We don't run it (it would
    open a browser); we just assert it's there and is callable.
    """
    from tradegy.dashboard.app import main
    assert callable(main)


def test_load_open_positions_callable():
    """The cached data loader exists and runs (registry is empty
    in the test env → returns empty list, which is the expected
    shape).
    """
    from tradegy.dashboard.app import _load_open_positions
    out = _load_open_positions()
    assert isinstance(out, list)


def test_load_recent_decisions_callable():
    from tradegy.dashboard.app import _load_recent_decisions
    out = _load_recent_decisions(n=3)
    assert isinstance(out, list)


def test_load_recent_cron_logs_callable():
    from tradegy.dashboard.app import _load_recent_cron_logs
    out = _load_recent_cron_logs(n=3)
    assert isinstance(out, list)
