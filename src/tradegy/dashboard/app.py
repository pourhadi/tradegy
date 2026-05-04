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
from datetime import datetime, timedelta, timezone
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


@st.cache_data(ttl=600)
def _load_iv_rank_trajectory(days: int = 90) -> list[dict]:
    """Compute ATM IV + rolling rank for the last `days` snapshots
    of spy_options_chain. Used by the IV-trajectory chart.

    Rank = (current - rolling_min) / (rolling_max - rolling_min)
    over the prior 252-day window — same definition used by the
    IvGatedStrategy wrapper so the chart's y-axis is directly
    comparable to the gate threshold.
    """
    from tradegy.options.chain_features import atm_iv
    from tradegy.options.chain_io import iter_chain_snapshots

    snaps = list(iter_chain_snapshots("spy_options_chain", ticker="SPY"))
    if not snaps:
        return []
    # Compute ATM IV over the FULL series so rank window has data.
    iv_series: list[tuple[str, float]] = []
    for snap in snaps:
        iv = atm_iv(snap, target_dte=30)
        if iv == iv:  # not NaN
            iv_series.append((snap.ts_utc.date().isoformat(), iv))
    if not iv_series:
        return []
    # Rolling rank (252-day window) — only emit the last `days`.
    out = []
    window = 252
    start_idx = max(window, len(iv_series) - days)
    for i in range(start_idx, len(iv_series)):
        date_str, iv = iv_series[i]
        wnd = [v for _, v in iv_series[max(0, i - window):i]]
        if not wnd:
            continue
        wmin, wmax = min(wnd), max(wnd)
        if wmax <= wmin:
            rank = 0.5
        else:
            rank = (iv - wmin) / (wmax - wmin)
        out.append({"date": date_str, "atm_iv": iv, "iv_rank": rank})
    return out


@st.cache_data(ttl=120)
def _load_cumulative_pnl_series() -> list[dict]:
    """Reconstruct daily P&L from the registry's open + close events.

    Each `close` row contributes `closed_pnl_per_share * multiplier
    * contracts` (when set). Sums by closed_ts date for a daily
    realized-P&L series; cumulative for the chart.
    """
    import json
    registry_path = config.repo_root() / "data" / "live_options" / "positions.jsonl"
    if not registry_path.exists():
        return []
    daily_pnl: dict[str, float] = {}
    open_meta: dict[str, dict] = {}
    with registry_path.open("r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row["type"] == "open":
                open_meta[row["position_id"]] = row
            elif row["type"] == "close":
                pid = row["position_id"]
                pnl_per_share = row.get("closed_pnl_per_share")
                if pnl_per_share is None or pid not in open_meta:
                    continue
                op = open_meta[pid]
                mult = op["legs"][0]["multiplier"] if op["legs"] else 100
                contracts = op["contracts"]
                pnl_dollars = float(pnl_per_share) * mult * contracts
                date_str = row["closed_ts"][:10]
                daily_pnl[date_str] = daily_pnl.get(date_str, 0.0) + pnl_dollars
    if not daily_pnl:
        return []
    sorted_dates = sorted(daily_pnl.keys())
    cum = 0.0
    out = []
    for d in sorted_dates:
        cum += daily_pnl[d]
        out.append({"date": d, "daily_pnl": daily_pnl[d], "cumulative_pnl": cum})
    return out


@st.cache_data(ttl=24 * 3600)
def _load_walk_forward_validation() -> dict:
    """Run the validated config's walk-forward and return per-
    window summary + gate decision. Cached for 24 hours — this
    is a 2-3 minute computation that we don't want hitting on
    every browser refresh.

    Re-runs automatically when the 24h TTL expires, OR when the
    operator clicks the "refresh validation" button on the Charts
    tab (which calls .clear() on this loader).
    """
    from tradegy.options.registry import resolve_strategy_ids
    from tradegy.options.risk import RiskManager, RiskConfig
    from tradegy.options.strategies import IvGatedStrategy
    from tradegy.options.strategy import ManagementRules
    from tradegy.options.walk_forward import (
        OptionsWalkForwardConfig,
        run_options_walk_forward,
    )

    bases = resolve_strategy_ids(
        "put_credit_spread_45dte_d30,iron_condor_45dte_d16,jade_lizard_45dte"
    )
    strategies = [
        IvGatedStrategy(base=s, max_iv_rank=0.25, window_days=252)
        for s in bases
    ]
    risk = RiskManager(RiskConfig(declared_capital=25_000.0))
    rules = ManagementRules(
        profit_take_pct=0.50, loss_stop_pct=2.0, dte_close=21,
    )
    cfg = OptionsWalkForwardConfig(
        train_years=3.0, test_years=1.0, roll_years=1.0,
    )

    # Determine coverage from latest ingested partition.
    raw_root = config.raw_dir() / "source=spy_options_chain"
    if not raw_root.exists():
        return {
            "error": "spy_options_chain not ingested",
            "windows": [],
        }
    dates = sorted(
        d.name.replace("date=", "")
        for d in raw_root.iterdir()
        if d.is_dir() and d.name.startswith("date=")
    )
    if not dates:
        return {"error": "no partitions", "windows": []}
    coverage_start = datetime(2020, 1, 1)
    latest_date = datetime.strptime(dates[-1], "%Y-%m-%d")
    # Add 1 day buffer so the rolling-window splitter doesn't
    # exclude the last partition.
    coverage_end = latest_date + timedelta(days=1)
    summary = run_options_walk_forward(
        strategies=strategies,
        source_id="spy_options_chain", ticker="SPY",
        coverage_start=coverage_start, coverage_end=coverage_end,
        config=cfg, risk=risk, rules=rules,
    )
    return {
        "passed": summary.passed,
        "fail_reason": summary.fail_reason,
        "avg_in_sample_sharpe": summary.avg_in_sample_sharpe,
        "avg_oos_sharpe": summary.avg_oos_sharpe,
        "worst_window_oos_sharpe": summary.worst_window_oos_sharpe,
        "avg_in_sample_trades": summary.avg_in_sample_trades,
        "avg_oos_trades": summary.avg_oos_trades,
        "windows": [
            {
                "index": w.index,
                "train_start": w.train_start.date().isoformat(),
                "train_end": w.train_end.date().isoformat(),
                "test_start": w.test_start.date().isoformat(),
                "test_end": w.test_end.date().isoformat(),
                "in_sample_trades": (
                    w.in_sample.n_closed_trades if w.in_sample else 0
                ),
                "in_sample_pnl": (
                    w.in_sample.realized_pnl_dollars if w.in_sample else 0.0
                ),
                "oos_trades": (
                    w.out_of_sample.n_closed_trades if w.out_of_sample else 0
                ),
                "oos_pnl": (
                    w.out_of_sample.realized_pnl_dollars if w.out_of_sample else 0.0
                ),
            }
            for w in summary.windows
        ],
        "computed_at": datetime.now(tz=timezone.utc).isoformat(),
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
    }


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


def _render_charts_tab() -> None:
    st.markdown("### IV-rank trajectory (last 90 days)")
    st.caption(
        "ATM IV (30 DTE) and its rolling 252-day percentile rank. "
        "Validated config trades when rank < 0.25 (red threshold). "
        "Counter-canonical: low-IV regimes are the vol-selling-friendly "
        "windows on SPY 2020-2025 (per doc 14)."
    )
    iv_data = _load_iv_rank_trajectory(days=90)
    if not iv_data:
        st.info(
            "no IV data — spy_options_chain not ingested or fewer than "
            "252 days of history."
        )
    else:
        df = pl.DataFrame(iv_data).to_pandas()
        # Two-axis chart: ATM IV (left) and IV rank (right with
        # gate threshold).
        rank_chart = alt.Chart(df).mark_line(color="#1f77b4").encode(
            x=alt.X("date:T", title="date"),
            y=alt.Y("iv_rank:Q", title="IV rank (rolling 252d)",
                    scale=alt.Scale(domain=[0, 1])),
            tooltip=["date:T", "atm_iv:Q", "iv_rank:Q"],
        )
        threshold = alt.Chart(pl.DataFrame(
            [{"y": 0.25}]
        ).to_pandas()).mark_rule(
            color="red", strokeDash=[4, 4],
        ).encode(y="y:Q")
        st.altair_chart(rank_chart + threshold, use_container_width=True)
        # Gate-status counts.
        n_below = sum(1 for r in iv_data if r["iv_rank"] < 0.25)
        n_total = len(iv_data)
        col1, col2, col3 = st.columns(3)
        col1.metric("days below gate (would trade)", f"{n_below} / {n_total}")
        col2.metric("latest IV rank", f"{iv_data[-1]['iv_rank']:.3f}")
        col3.metric(
            "latest ATM IV",
            f"{iv_data[-1]['atm_iv']:.3f}",
            f"{iv_data[-1]['atm_iv'] * 100:.1f}%",
        )

    st.divider()
    st.markdown("### walk-forward validation (current data)")
    st.caption(
        "Re-runs the validated config (PCS+IC+JL+IV<0.25, $25K, "
        "default mgmt) over a 3y/1y/1y rolling window against ALL "
        "ingested SPY data — including any 2026 partitions added "
        "since the original validation. Tells you whether the gate "
        "still passes as the live regime drifts. Cached 24h."
    )

    col_run, _ = st.columns([1, 4])
    with col_run:
        if st.button(
            "🔁 refresh validation (~3 min)",
            use_container_width=True,
        ):
            _load_walk_forward_validation.clear()
            st.rerun()

    try:
        wf = _load_walk_forward_validation()
    except Exception as exc:  # noqa: BLE001
        st.error(f"walk-forward error: {type(exc).__name__}: {exc}")
        wf = None

    if wf is None:
        pass
    elif wf.get("error"):
        st.warning(f"validation skipped: {wf['error']}")
    elif not wf["windows"]:
        st.warning("no walk-forward windows generated (coverage too short)")
    else:
        gate_emoji = "✅" if wf["passed"] else "❌"
        gate_text = "PASS" if wf["passed"] else f"FAIL — {wf['fail_reason']}"
        st.markdown(f"**gate**: {gate_emoji} {gate_text}")
        st.caption(
            f"computed {wf['computed_at']} from coverage "
            f"{wf['coverage_start'][:10]} → {wf['coverage_end'][:10]}"
        )
        col1, col2, col3 = st.columns(3)
        col1.metric(
            "avg IS Sharpe", f"{wf['avg_in_sample_sharpe']:+.3f}",
        )
        col2.metric(
            "avg OOS Sharpe", f"{wf['avg_oos_sharpe']:+.3f}",
        )
        col3.metric(
            "worst-window OOS Sharpe",
            f"{wf['worst_window_oos_sharpe']:+.3f}",
        )

        # Per-window Sharpe bar chart.
        win_rows = []
        for w in wf["windows"]:
            win_rows.append({
                "window": f"win {w['index']}: {w['test_start']}→{w['test_end']}",
                "kind": "OOS",
                "pnl": w["oos_pnl"],
                "trades": w["oos_trades"],
            })
            win_rows.append({
                "window": f"win {w['index']}: {w['test_start']}→{w['test_end']}",
                "kind": "IS",
                "pnl": w["in_sample_pnl"],
                "trades": w["in_sample_trades"],
            })
        win_df = pl.DataFrame(win_rows).to_pandas()
        chart = alt.Chart(win_df).mark_bar().encode(
            x=alt.X("window:N", title="window"),
            y=alt.Y("pnl:Q", title="realized P&L ($)"),
            color=alt.Color(
                "kind:N",
                scale=alt.Scale(
                    domain=["IS", "OOS"],
                    range=["#888888", "#2ca02c"],
                ),
            ),
            xOffset="kind:N",
            tooltip=["window", "kind", "pnl", "trades"],
        )
        st.altair_chart(chart, use_container_width=True)

        # Detail table.
        st.dataframe(
            pl.DataFrame(wf["windows"]).to_pandas(),
            use_container_width=True,
            column_config={
                "in_sample_pnl": st.column_config.NumberColumn(format="$%.0f"),
                "oos_pnl": st.column_config.NumberColumn(format="$%.0f"),
            },
        )

    st.divider()
    st.markdown("### cumulative realized P&L (registry)")
    st.caption(
        "Daily and cumulative realized P&L from the position "
        "registry's `close` events (positions actually closed at "
        "the broker). Entries that haven't closed yet are not in "
        "this chart — those are unrealized."
    )
    pnl_data = _load_cumulative_pnl_series()
    if not pnl_data:
        st.info(
            "no closed-trade history yet — positions in the registry "
            "either haven't closed or the cron hasn't recorded "
            "closed_pnl_per_share."
        )
    else:
        df = pl.DataFrame(pnl_data).to_pandas()
        cum_chart = alt.Chart(df).mark_line(color="#2ca02c").encode(
            x=alt.X("date:T", title="closed date"),
            y=alt.Y("cumulative_pnl:Q", title="cumulative P&L ($)"),
            tooltip=["date:T", "daily_pnl:Q", "cumulative_pnl:Q"],
        )
        st.altair_chart(cum_chart, use_container_width=True)
        latest = pnl_data[-1]["cumulative_pnl"]
        col1, col2 = st.columns(2)
        col1.metric("cumulative realized $", f"${latest:+,.0f}")
        col2.metric("# of P&L days", f"{len(pnl_data)}")


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
    """V2 control panel — inline buttons for the most common
    operator actions. Shells out to the CLI via subprocess and
    streams output back into the dashboard.
    """
    import os

    st.markdown("### actions")

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("🔄 generate decision (no route)", use_container_width=True):
            with st.spinner("running tradegy live-options..."):
                out = _run_cli(["uv", "run", "tradegy", "live-options"])
            st.code(out, language="text")
            st.cache_data.clear()
    with col2:
        st.button(
            "🚀 generate + route to IBKR",
            help="Routes entries + V2 close loop to IBKR paper. "
                 "Requires --paper-account env or click toggles below.",
            disabled=not _route_safe(),
            use_container_width=True,
            on_click=_route_now,
        )
        if not _route_safe():
            st.caption("⚠️ disabled: env IBKR_PAPER_ACCOUNT not set")
    with col3:
        if st.button("📋 doctor", use_container_width=True):
            _run_doctor_checks.clear()
            st.rerun()

    st.divider()
    st.markdown("### cron control")
    col1, col2, col3 = st.columns(3)
    plist_target = (
        Path.home() / "Library" / "LaunchAgents" / "com.tradegy.live-options.plist"
    )
    cron_loaded = subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True,
    ).stdout
    is_loaded = "com.tradegy.live-options" in cron_loaded
    with col1:
        st.metric(
            "cron status",
            "🟢 loaded" if is_loaded else "🔴 not loaded",
        )
    with col2:
        if is_loaded and st.button(
            "⏸️ pause cron (unload)", use_container_width=True,
        ):
            out = _run_cli(
                ["launchctl", "unload", str(plist_target)],
                use_uv=False,
            )
            st.code(out or "(no output — unload ok)", language="text")
            st.rerun()
    with col3:
        if (not is_loaded) and st.button(
            "▶️ resume cron (load)", use_container_width=True,
        ):
            out = _run_cli(
                ["launchctl", "load", str(plist_target)],
                use_uv=False,
            )
            st.code(out or "(no output — load ok)", language="text")
            st.rerun()

    st.divider()
    st.markdown("### data refresh")
    col1, col2 = st.columns(2)
    with col1:
        if st.button(
            "📥 pull today's SPY chain + ingest",
            use_container_width=True,
            disabled=not os.environ.get("ORATS_API_KEY"),
        ):
            from datetime import date as _date
            today = _date.today().isoformat()
            with st.spinner(
                f"pulling SPY {today} (1 trade day)..."
            ):
                pull_out = _run_cli(
                    ["python",
                     "/Users/dan/code/data/download_spx_options_orats.py",
                     "--ticker", "SPY",
                     "--start", today, "--end", today,
                     "--confirm", "--resume"],
                    use_uv=False,
                )
            st.code(pull_out, language="text")
            with st.spinner("ingesting..."):
                ingest_out = _run_cli([
                    "uv", "run", "tradegy", "ingest",
                    "/Users/dan/code/data/spy_options_orats/spy_options_orats.csv",
                    "--source-id", "spy_options_chain",
                ])
            st.code(ingest_out, language="text")
            st.cache_data.clear()
        if not os.environ.get("ORATS_API_KEY"):
            st.caption("⚠️ disabled: env ORATS_API_KEY not set")
    with col2:
        meta = _load_latest_snapshot_meta()
        if meta:
            st.metric(
                "latest chain partition",
                meta["date"],
                f"{meta['age_days']}d old, "
                f"{meta['n_partitions']} partitions",
            )
        else:
            st.error("no chain partitions ingested")

    st.divider()
    st.markdown(
        "Equivalent CLI commands (for reference):\n"
        "- `uv run tradegy live-options`\n"
        "- `uv run tradegy live-options --route --paper-account $IBKR_PAPER_ACCOUNT`\n"
        "- `uv run tradegy live-options-doctor`\n"
        "- `launchctl unload ~/Library/LaunchAgents/com.tradegy.live-options.plist`\n"
        "- `launchctl load ~/Library/LaunchAgents/com.tradegy.live-options.plist`"
    )


def _route_safe() -> bool:
    """Don't enable the route button unless paper account env is set."""
    import os
    return bool(os.environ.get("IBKR_PAPER_ACCOUNT"))


def _route_now() -> None:
    """on_click handler for the route button. Runs live-options
    --route synchronously and stuffs output into session_state for
    re-render. Streamlit can't render in on_click directly.
    """
    import os
    paper = os.environ.get("IBKR_PAPER_ACCOUNT", "")
    out = _run_cli([
        "uv", "run", "tradegy", "live-options",
        "--route", "--paper-account", paper,
    ])
    st.session_state["last_route_output"] = out
    st.cache_data.clear()


def _run_cli(args: list[str], *, use_uv: bool = True) -> str:
    """Run a CLI command and return combined stdout/stderr.

    Times out after 5 minutes. Long output is truncated with a
    sentinel.
    """
    import os
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True,
            env=os.environ.copy(),
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return "[command timed out after 5 minutes]"
    out = (proc.stdout or "") + (proc.stderr or "")
    if len(out) > 20000:
        out = out[:10000] + f"\n\n[... truncated, full {len(out)} chars ...]\n\n" + out[-10000:]
    return out


# ── Main ─────────────────────────────────────────────────────────


def main() -> None:
    _setup_page()
    _render_top_strip()
    st.divider()
    tab_pos, tab_dec, tab_chart, tab_sess, tab_doc, tab_ctl = st.tabs(
        ["positions", "today's decision", "charts",
         "recent sessions", "doctor", "controls"]
    )
    with tab_pos:
        _render_positions_tab()
    with tab_dec:
        _render_decision_tab()
    with tab_chart:
        _render_charts_tab()
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
