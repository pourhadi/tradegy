"""Install-verification helper for the live-options stack.

Walks through every install-side check the operator needs to pass
before the daily cron will produce reliable results. Each check is
INDEPENDENT — one check failing doesn't stop the others — so the
operator can fix issues in parallel and re-run the doctor to see
what's still broken.

Checks (in order; printed sequentially):

  1. ENV — required environment variables (ORATS_API_KEY,
     IBKR_PAPER_ACCOUNT). PASS if both set; FAIL otherwise.
  2. WRAPPER — scripts/live_options_daily.sh exists, executable,
     bash-syntax-clean. PASS if all three; FAIL otherwise.
  3. PLIST — scripts/com.tradegy.live-options.plist syntactically
     valid (plutil -lint), and report whether it's currently loaded
     in launchctl. PASS if syntactically valid; WARNING if not
     loaded (the operator may not have installed yet, but install
     is a one-step deferred action).
  4. TWS_PORT — TCP socket reachable on IBKR_HOST:IBKR_PORT
     (default 127.0.0.1:7497). PASS if reachable; FAIL with the
     "TWS not running / wrong port" remediation.
  5. IBKR_PROBE — full IBKRConnection.connect() + managedAccounts
     + disconnect. PASS if --paper-account in accounts list;
     FAIL with the actual accounts list for diagnosis.
  6. ORATS_PROBE — small test request via the existing download
     script's dry-run. PASS if API key is accepted (cost-check
     returns); FAIL with the API error message.
  7. CHAIN_FRESHNESS — when was the spy_options_chain last
     ingested? PASS if within last 7 calendar days (the cron
     should run weekdays, so 7 calendar days = 5 trade days
     buffer); WARNING if older.
  8. REGISTRY_SANITY — load_open_positions() returns without
     error, count is shown. PASS regardless of count (empty
     registry is fine for a fresh install).

Each check returns a `CheckResult` so a programmatic caller (the
dashboard) can render the same status without re-running the CLI
output formatter.
"""
from __future__ import annotations

import os
import socket
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from tradegy import config


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one doctor check.

    `status` is one of "pass" | "fail" | "warning" | "skip".
    `message` is a single-line summary; `detail` is multiline
    diagnostic info (printed indented under the message).
    """

    name: str
    status: str
    message: str
    detail: str = ""


def _check_env() -> CheckResult:
    missing = []
    if not os.environ.get("ORATS_API_KEY"):
        missing.append("ORATS_API_KEY")
    if not os.environ.get("IBKR_PAPER_ACCOUNT"):
        missing.append("IBKR_PAPER_ACCOUNT")
    if missing:
        return CheckResult(
            name="ENV", status="fail",
            message=f"missing env vars: {missing}",
            detail=(
                "Set in ~/.zprofile:\n"
                "  export ORATS_API_KEY=...\n"
                "  export IBKR_PAPER_ACCOUNT=DU7535411\n"
                "Then `source ~/.zprofile` and re-run."
            ),
        )
    return CheckResult(
        name="ENV", status="pass",
        message="ORATS_API_KEY + IBKR_PAPER_ACCOUNT set",
        detail=f"  IBKR_PAPER_ACCOUNT={os.environ['IBKR_PAPER_ACCOUNT']}",
    )


def _check_wrapper() -> CheckResult:
    p = config.repo_root() / "scripts" / "live_options_daily.sh"
    if not p.exists():
        return CheckResult(
            name="WRAPPER", status="fail",
            message=f"{p} not found",
        )
    if not os.access(p, os.X_OK):
        return CheckResult(
            name="WRAPPER", status="fail",
            message=f"{p} not executable",
            detail=f"  fix: chmod +x {p}",
        )
    syntax = subprocess.run(
        ["bash", "-n", str(p)], capture_output=True, text=True,
    )
    if syntax.returncode != 0:
        return CheckResult(
            name="WRAPPER", status="fail",
            message="shell-syntax error",
            detail=syntax.stderr,
        )
    return CheckResult(
        name="WRAPPER", status="pass",
        message=f"{p.name} exists, executable, syntax-clean",
    )


def _check_plist() -> CheckResult:
    p = config.repo_root() / "scripts" / "com.tradegy.live-options.plist"
    if not p.exists():
        return CheckResult(
            name="PLIST", status="fail",
            message=f"{p} not found",
        )
    lint = subprocess.run(
        ["plutil", "-lint", str(p)], capture_output=True, text=True,
    )
    if lint.returncode != 0:
        return CheckResult(
            name="PLIST", status="fail",
            message="plist syntax error",
            detail=lint.stdout + lint.stderr,
        )
    # Check if loaded in launchctl.
    listed = subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True,
    )
    loaded = "com.tradegy.live-options" in listed.stdout
    if loaded:
        return CheckResult(
            name="PLIST", status="pass",
            message="plist syntax ok, loaded in launchctl",
        )
    return CheckResult(
        name="PLIST", status="warning",
        message="plist syntax ok, NOT loaded in launchctl",
        detail=(
            "  install with:\n"
            "    cp scripts/com.tradegy.live-options.plist "
            "~/Library/LaunchAgents/\n"
            "    launchctl load "
            "~/Library/LaunchAgents/com.tradegy.live-options.plist"
        ),
    )


def _check_tws_port() -> CheckResult:
    host = os.environ.get("IBKR_HOST", "127.0.0.1")
    port = int(os.environ.get("IBKR_PORT", "7497"))
    try:
        with socket.create_connection((host, port), timeout=2.0):
            pass
    except (socket.timeout, ConnectionRefusedError, OSError) as exc:
        return CheckResult(
            name="TWS_PORT", status="fail",
            message=f"TWS port {host}:{port} not reachable: "
                    f"{type(exc).__name__}",
            detail=(
                "  Make sure TWS or IB Gateway is running, logged in, "
                "and the\n"
                "  API socket port matches IBKR_PORT (paper TWS = 7497, "
                "live = 7496).\n"
                "  Configure: TWS → File → Global Configuration → API → "
                "Settings."
            ),
        )
    return CheckResult(
        name="TWS_PORT", status="pass",
        message=f"{host}:{port} reachable",
    )


def _check_ibkr_probe() -> CheckResult:
    """Full connect → managedAccounts → disconnect."""
    import asyncio
    from tradegy.live.ibkr import IBKRConnection

    paper = os.environ.get("IBKR_PAPER_ACCOUNT", "")

    async def _do() -> tuple[bool, str, list[str]]:
        conn = IBKRConnection()
        try:
            await conn.connect()
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}", []
        try:
            try:
                accounts = list(conn.ib.managedAccounts())
            except Exception:  # noqa: BLE001
                accounts = []
            return True, "", accounts
        finally:
            await conn.disconnect()

    try:
        ok, err, accounts = asyncio.run(_do())
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="IBKR_PROBE", status="fail",
            message=f"{type(exc).__name__}: {exc}",
        )
    if not ok:
        return CheckResult(
            name="IBKR_PROBE", status="fail",
            message=f"connection failed: {err}",
        )
    if paper and paper not in accounts:
        return CheckResult(
            name="IBKR_PROBE", status="fail",
            message=f"paper account {paper!r} not in managed accounts",
            detail=f"  managed_accounts={accounts}",
        )
    return CheckResult(
        name="IBKR_PROBE", status="pass",
        message=f"connected, accounts={accounts}",
    )


def _check_orats_probe() -> CheckResult:
    """Small request via the download script's dry-run path."""
    from datetime import date as _d
    today = _d.today().isoformat()
    script = Path("/Users/dan/code/data/download_spx_options_orats.py")
    if not script.exists():
        return CheckResult(
            name="ORATS_PROBE", status="warning",
            message="download script not found, skipping ORATS probe",
            detail=f"  {script}",
        )
    api_key = os.environ.get("ORATS_API_KEY", "")
    if not api_key:
        return CheckResult(
            name="ORATS_PROBE", status="skip",
            message="ORATS_API_KEY not set (caught by ENV check)",
        )
    # Dry-run (no --confirm) just prints ETA; don't actually fetch.
    out = subprocess.run(
        ["python", str(script),
         "--ticker", "SPY",
         "--start", today, "--end", today],
        capture_output=True, text=True,
        env={**os.environ, "ORATS_API_KEY": api_key},
        timeout=10.0,
    )
    if out.returncode != 0:
        return CheckResult(
            name="ORATS_PROBE", status="fail",
            message=f"download script returned {out.returncode}",
            detail=(out.stderr or out.stdout)[:500],
        )
    return CheckResult(
        name="ORATS_PROBE", status="pass",
        message="download script dry-run ok",
    )


def _check_chain_freshness() -> CheckResult:
    raw_root = config.raw_dir() / "source=spy_options_chain"
    if not raw_root.exists():
        return CheckResult(
            name="CHAIN_FRESHNESS", status="fail",
            message="spy_options_chain not yet ingested",
            detail=(
                "  Run an initial pull + ingest:\n"
                "    python /Users/dan/code/data/download_spx_options_orats.py "
                "--ticker SPY --start 2020-01-01 --end <today> --confirm\n"
                "    uv run tradegy ingest "
                "/Users/dan/code/data/spy_options_orats/spy_options_orats.csv "
                "--source-id spy_options_chain"
            ),
        )
    # Find newest date= partition.
    dates = sorted(
        d.name.replace("date=", "")
        for d in raw_root.iterdir() if d.is_dir() and d.name.startswith("date=")
    )
    if not dates:
        return CheckResult(
            name="CHAIN_FRESHNESS", status="fail",
            message="spy_options_chain present but no partitions",
        )
    newest = datetime.strptime(dates[-1], "%Y-%m-%d").date()
    age = (datetime.now(tz=timezone.utc).date() - newest).days
    if age > 7:
        return CheckResult(
            name="CHAIN_FRESHNESS", status="warning",
            message=f"latest snapshot is {age} days old (newest={newest})",
            detail="  Cron may not have run recently — check cron_logs/.",
        )
    return CheckResult(
        name="CHAIN_FRESHNESS", status="pass",
        message=f"latest snapshot {newest} ({age}d old)",
    )


def _check_registry_sanity() -> CheckResult:
    from tradegy.live.options_position_registry import load_open_positions
    try:
        positions = load_open_positions()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="REGISTRY", status="fail",
            message=f"load_open_positions raised: "
                    f"{type(exc).__name__}: {exc}",
        )
    return CheckResult(
        name="REGISTRY", status="pass",
        message=f"{len(positions)} open position(s) registered",
    )


_CHECKS: list[Callable[[], CheckResult]] = [
    _check_env,
    _check_wrapper,
    _check_plist,
    _check_tws_port,
    _check_ibkr_probe,
    _check_orats_probe,
    _check_chain_freshness,
    _check_registry_sanity,
]


def run_all_checks(*, skip_ibkr: bool = False) -> list[CheckResult]:
    """Run every check in sequence; return all results.

    `skip_ibkr=True` skips checks that require a TWS connection
    (TWS_PORT and IBKR_PROBE) — useful for quick sanity checks
    when you know TWS is intentionally down.
    """
    out: list[CheckResult] = []
    for check in _CHECKS:
        if skip_ibkr and check.__name__ in {"_check_tws_port", "_check_ibkr_probe"}:
            out.append(CheckResult(
                name=check.__name__.replace("_check_", "").upper(),
                status="skip", message="--skip-ibkr",
            ))
            continue
        try:
            out.append(check())
        except Exception as exc:  # noqa: BLE001
            out.append(CheckResult(
                name=check.__name__.replace("_check_", "").upper(),
                status="fail",
                message=f"check itself raised: "
                        f"{type(exc).__name__}: {exc}",
            ))
    return out
