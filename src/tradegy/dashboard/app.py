"""Streamlit dashboard for live-options monitoring + control.

Single-page layout, auto-refresh every 60 seconds:

  TOP STRIP (3 columns):
    - System status: doctor checks (pass/fail/warn counts), latest
      cron exit code, last-session timestamp.
    - Position summary: # open positions, total capital at risk,
      total unrealized P&L, # triggered for close.
    - Latest decision: snapshot date, IV-rank value vs threshold,
      # entry candidates, link to JSON file.

  MAIN PANEL — TABS:
    1. Open positions — full registry view marked against latest
       chain. Same data the `live-options-status` CLI shows but
       with sortable columns and color-coded P&L.
    2. Today's decision — the JSON contents rendered as a tree;
       per-leg bid/ask/IV at decision time so the operator can
       sanity-check fillability before approving.
    3. Recent sessions — table of the last 10 sessions: date,
       entries placed, closes routed, reconciliation status,
       cron exit code.
    4. Walk-forward validation — re-runs the validated config
       against ingested data (button-triggered; long-running) and
       shows the gate result + per-window summary.
    5. Controls — run-now button, pause-cron toggle, manual
       backfill upload form.

  SIDEBAR:
    - Filters / overrides for the validated config (IV gate, mgmt
      rules) — these don't persist; they let you preview what a
      different config WOULD do without modifying the cron.

Launch:
    uv run tradegy live-options-dashboard
    # opens browser at http://localhost:8501

The CLI command in tradegy.cli wraps streamlit run on this file
so the operator doesn't need to know streamlit-specifics.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import altair as alt
import polars as pl
import streamlit as st

from tradegy import config


# ── Page setup ────────────────────────────────────────────────────


def _setup_page() -> None:
    st.set_page_config(
        page_title="tradegy — live-options",
        page_icon="📈",
        layout="wide",
    )
    st.title("tradegy — live-options")
    st.caption(
        "monitoring + control for the validated SPY+PCS+IC+JL+IV<0.25 "
        "portfolio at $25K. see "
        "[doc 14](trading_platform_docs/14_options_volatility_selling.md) + "
        "[runbook](trading_platform_docs/15_live_options_runbook.md)."
    )


# ── Data loaders (cached) ─────────────────────────────────────────


@st.cache_data(ttl=30)
def _load_open_positions() -> list[dict]:
    """Return registry positions as a list of dicts. Cached 30s."""
    from tradegy.live.options_position_registry import load_open_positions
    positions = load_open_positions()
    out = []
    for p in positions:
        out.append({
            "position_id": p.position_id,
            "strategy": p.strategy_class,
            "contracts": p.contracts,
            "n_legs": len(p.legs),
            "entry_credit_$": p.entry_credit_dollars,
            "max_loss_$": p.total_capital_at_risk,
            "entry_ts": p.entry_ts,
            "nearest_expiry": min(p.expiries),
            # Each leg's strike for quick visual.
            "legs": " ".join(
                f"{l.side.value[0].upper()}{l.quantity:+d}@{l.strike:.0f}"
                for l in p.legs
            ),
        })
    return out


@st.cache_data(ttl=60)
def _load_latest_snapshot_meta() -> dict | None:
    """Latest spy_options_chain partition metadata."""
    raw_root = config.raw_dir() / "source=spy_options_chain"
    if not raw_root.exists():
        return None
    dates = sorted(
        d.name.replace("date=", "")
        for d in raw_root.iterdir() if d.is_dir() and d.name.startswith("date=")
    )
    if not dates:
        return None
    newest = dates[-1]
    age = (datetime.now(tz=timezone.utc).date() -
           datetime.strptime(newest, "%Y-%m-%d").date()).days
    return {"date": newest, "age_days": age, "n_partitions": len(dates)}


@st.cache_data(ttl=60)
def _load_recent_decisions(n: int = 10) -> list[dict]:
    """Last N decision JSON files, newest first."""
    decisions_dir = config.repo_root() / "data" / "live_options" / "decisions"
    if not decisions_dir.exists():
        return []
    files = sorted(decisions_dir.glob("*.json"), reverse=True)[:n]
    out = []
    for f in files:
        try:
            payload = json.loads(f.read_text())
            out.append({
                "filename": f.name,
                "snapshot_date": payload.get("snapshot_ts_utc", "")[:10],
                "underlying_price": payload.get("snapshot_underlying_price", 0.0),
                "n_entries": len(payload.get("entries", [])),
                "n_replayed_snapshots": payload.get("n_replayed_snapshots", 0),
                "iv_gate_max": payload.get("iv_gate_max"),
                "replay_realized_pnl": payload.get("replay_realized_pnl", 0.0),
                "routing_results": payload.get("routing_results", []),
                "raw": payload,
            })
        except Exception:  # noqa: BLE001
            pass
    return out


@st.cache_data(ttl=60)
def _load_recent_cron_logs(n: int = 5) -> list[dict]:
    """Tail of the last N cron log files."""
    log_dir = config.repo_root() / "data" / "live_options" / "cron_logs"
    if not log_dir.exists():
        return []
    files = sorted(
        log_dir.glob("*.log"), reverse=True,
    )[:n]
    out = []
    for f in files:
        try:
            content = f.read_text()
            success = "=== SUCCESS" in content
            failed = "FAIL:" in content
            tail = "\n".join(content.splitlines()[-20:])
            out.append({
                "filename": f.name,
                "size_kb": f.stat().st_size / 1024,
                "success": success,
                "failed": failed,
                "tail": tail,
            })
        except Exception:  # noqa: BLE001
            pass
    return out


@st.cache_data(ttl=120)
def _run_doctor_checks() -> list[dict]:
    """Run the install doctor; return results as plain dicts."""
    from tradegy.live.options_doctor import run_all_checks
    results = run_all_checks(skip_ibkr=False)
    return [
        {"name": r.name, "status": r.status,
         "message": r.message, "detail": r.detail}
        for r in results
    ]


# ── Position table with live mark ─────────────────────────────────


def _load_position_statuses_with_mark() -> list[dict]:
    """Mark every registered position against the latest snapshot
    and evaluate close triggers. Returns dicts ready for st.dataframe.
    """
    from tradegy.live.options_orchestrator import compute_position_statuses
    from tradegy.options.chain_io import iter_chain_snapshots
    from tradegy.options.strategy import ManagementRules

    snaps = list(iter_chain_snapshots("spy_options_chain", ticker="SPY"))
    if not snaps:
        return []
    snap = snaps[-1]
    rules = ManagementRules(profit_take_pct=0.50, loss_stop_pct=2.0, dte_close=21)
    statuses = compute_position_statuses(snapshot=snap, rules=rules)
    out = []
    for s in statuses:
        out.append({
            "position_id": s.position_id,
            "strategy": s.strategy_class,
            "legs": s.leg_summary,
            "DTE": s.days_to_expiry,
            "entry $": s.entry_credit_dollars,
            "mark $": s.mark_dollars,
            "pnl % credit": (
                s.pnl_pct_of_max_credit * 100
                if s.pnl_pct_of_max_credit == s.pnl_pct_of_max_credit
                else None
            ),
            "trigger": s.triggered_close_reason or "—",
        })
    return out


# ── UI sections ──────────────────────────────────────────────────


def _render_top_strip() -> None:
    col1, col2, col3 = st.columns(3)

    with col1:
        st.subheader("system")
        doctor = _run_doctor_checks()
        n_pass = sum(1 for r in doctor if r["status"] == "pass")
        n_fail = sum(1 for r in doctor if r["status"] == "fail")
        n_warn = sum(1 for r in doctor if r["status"] == "warning")
        st.metric("doctor pass / fail / warn",
                  f"{n_pass} / {n_fail} / {n_warn}")
        # Latest cron status.
        logs = _load_recent_cron_logs(n=1)
        if logs:
            cron_status = (
                "✅ ok" if logs[0]["success"] else
                "❌ failed" if logs[0]["failed"] else
                "🟡 ran (no clear status)"
            )
            st.caption(f"latest cron: {logs[0]['filename']} — {cron_status}")
        else:
            st.caption("latest cron: never run")

    with col2:
        st.subheader("positions")
        positions = _load_open_positions()
        total_capital = sum(p["max_loss_$"] for p in positions)
        st.metric(
            "open positions",
            f"{len(positions)}",
            f"${total_capital:,.0f} at risk" if positions else None,
        )
        # Marked P&L for triggered count.
        statuses = _load_position_statuses_with_mark()
        n_triggered = sum(1 for s in statuses if s["trigger"] != "—")
        if n_triggered > 0:
            st.warning(
                f"{n_triggered} position(s) will close on next "
                "live-options --route session"
            )

    with col3:
        st.subheader("latest decision")
        decisions = _load_recent_decisions(n=1)
        if decisions:
            d = decisions[0]
            st.metric(
                f"snapshot {d['snapshot_date']}",
                f"{d['n_entries']} entries",
                f"SPY=${d['underlying_price']:.2f}, "
                f"IV<{d['iv_gate_max']}",
            )
            st.caption(d["filename"])
        else:
            st.caption("no decisions written yet")


def _render_positions_tab() -> None:
    statuses = _load_position_statuses_with_mark()
    if not statuses:
        st.info("no open positions in the registry")
        return
    df = pl.DataFrame(statuses).to_pandas()
    st.dataframe(
        df,
        use_container_width=True,
        column_config={
            "pnl % credit": st.column_config.NumberColumn(format="%.1f%%"),
            "entry $": st.column_config.NumberColumn(format="$%.0f"),
            "mark $": st.column_config.NumberColumn(format="$%.0f"),
        },
    )


def _render_decision_tab() -> None:
    decisions = _load_recent_decisions(n=1)
    if not decisions:
        st.info("no decision file yet — run `tradegy live-options`")
        return
    d = decisions[0]["raw"]
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(f"**snapshot**: `{d['snapshot_ts_utc']}`")
        st.markdown(f"**SPY**: ${d['snapshot_underlying_price']:.2f}")
        st.markdown(f"**IV gate**: < {d['iv_gate_max']}")
        st.markdown(f"**capital**: ${d['declared_capital']:,.0f}")
    with col2:
        st.markdown(f"**replayed snapshots**: {d['n_replayed_snapshots']}")
        st.markdown(f"**replay realized P&L**: ${d['replay_realized_pnl']:,.0f}")
        st.markdown(f"**open at replay end**: {d['replay_open_positions_at_today']}")
    st.divider()
    if not d["entries"]:
        st.info("no entry candidates today (IV gate blocking, or no signal)")
        return
    st.markdown(f"### entry candidates ({len(d['entries'])})")
    for entry in d["entries"]:
        with st.expander(f"{entry['strategy_id']} ×{entry['contracts']}"):
            leg_df = pl.DataFrame(entry["legs"]).to_pandas()
            st.dataframe(leg_df, use_container_width=True)


def _render_sessions_tab() -> None:
    decisions = _load_recent_decisions(n=10)
    logs = _load_recent_cron_logs(n=5)
    st.markdown("### recent decisions")
    if decisions:
        rows = [
            {
                "snapshot_date": d["snapshot_date"],
                "underlying_price": d["underlying_price"],
                "n_entries": d["n_entries"],
                "n_routed_ok": sum(
                    1 for r in d["routing_results"] if r.get("accepted")
                ),
                "filename": d["filename"],
            }
            for d in decisions
        ]
        st.dataframe(
            pl.DataFrame(rows).to_pandas(), use_container_width=True,
        )
    else:
        st.caption("no decisions yet")
    st.markdown("### recent cron logs")
    if not logs:
        st.caption("no cron logs yet — cron not installed or never run")
        return
    for log in logs:
        with st.expander(
            f"{log['filename']}  ({log['size_kb']:.1f} KB)  — "
            f"{'✅ ok' if log['success'] else '❌ failed' if log['failed'] else '🟡'}"
        ):
            st.code(log["tail"], language="text")


def _render_doctor_tab() -> None:
    doctor = _run_doctor_checks()
    rows = [
        {"check": r["name"], "status": r["status"], "message": r["message"]}
        for r in doctor
    ]
    st.dataframe(pl.DataFrame(rows).to_pandas(), use_container_width=True)
    for r in doctor:
        if r["detail"] and r["status"] in {"fail", "warning"}:
            with st.expander(f"{r['name']} detail"):
                st.code(r["detail"], language="text")


def _render_controls_tab() -> None:
    st.markdown(
        "**Read-only V1**: control buttons are disabled in this version. "
        "Use the CLI for actions:\n\n"
        "- `tradegy live-options` (decision-only)\n"
        "- `tradegy live-options --route --paper-account $IBKR_PAPER_ACCOUNT`\n"
        "- `tradegy live-options-status`\n"
        "- `tradegy live-options-doctor`\n"
        "- `launchctl unload ~/Library/LaunchAgents/com.tradegy.live-options.plist` (pause cron)\n"
        "- `launchctl load ~/Library/LaunchAgents/com.tradegy.live-options.plist` (resume cron)\n"
    )
    st.divider()
    st.markdown(
        "V2 of this dashboard will add inline controls for the above. "
        "The current read-only V1 already shows everything you need to "
        "decide whether to run a manual session."
    )


# ── Main ─────────────────────────────────────────────────────────


def main() -> None:
    _setup_page()
    _render_top_strip()
    st.divider()
    tab_pos, tab_dec, tab_sess, tab_doc, tab_ctl = st.tabs(
        ["positions", "today's decision", "recent sessions",
         "doctor", "controls"]
    )
    with tab_pos:
        _render_positions_tab()
    with tab_dec:
        _render_decision_tab()
    with tab_sess:
        _render_sessions_tab()
    with tab_doc:
        _render_doctor_tab()
    with tab_ctl:
        _render_controls_tab()
    st.caption(
        f"data cached briefly (30-120s). reload to re-fetch. "
        f"server time: {datetime.now(tz=timezone.utc).isoformat()}"
    )


if __name__ == "__main__":
    main()
