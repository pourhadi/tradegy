"""Streamlit dashboard for the live MES 0DTE paper-trading daemon.

Reads from the daemon's on-disk artifacts (no IBKR connection
needed):

    data/live_options/mes_0dte_entries/<YYYY-MM-DD>.json
        — one JSON file per session the daemon entered.  Contains
          entry timestamp, strikes, leg quotes, and close metadata
          (close_reason, close_credit, pnl_per_share) once the
          management process closes the position.

    data/live_options/mes_0dte_logs/<YYYY-MM-DD>_entry.log
    data/live_options/mes_0dte_logs/<YYYY-MM-DD>_manage.log
        — operator-readable wrapper-shell logs.

Run:
    uv run streamlit run scripts/mes_0dte_dashboard.py

The dashboard auto-refreshes every 30 sec so it stays current
during the trading session.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st


REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRY_RECORDS_DIR = REPO_ROOT / "data" / "live_options" / "mes_0dte_entries"
LOG_DIR = REPO_ROOT / "data" / "live_options" / "mes_0dte_logs"
KILL_SWITCH_FILE = REPO_ROOT / "data" / "live_options" / "MES_0DTE_KILL"

MULTIPLIER = 5  # MES futures-options $/point multiplier


# ── Page config ─────────────────────────────────────────────────


st.set_page_config(
    page_title="MES 0DTE Live Paper",
    page_icon="📈",
    layout="wide",
)

# Auto-refresh every 30 sec.  Streamlit's recommended pattern is to
# use st.empty() + a sleep loop, but the simpler `st.rerun()` after
# a delay is fine for a dashboard that just reads files.
REFRESH_SECONDS = 30


# ── Data loading ───────────────────────────────────────────────


@st.cache_data(ttl=REFRESH_SECONDS)
def load_entry_records() -> list[dict]:
    """Load every JSON entry record on disk, sorted by session date."""
    if not ENTRY_RECORDS_DIR.exists():
        return []
    records = []
    for f in sorted(ENTRY_RECORDS_DIR.glob("*.json")):
        try:
            records.append(json.loads(f.read_text()))
        except Exception:
            continue
    return records


def _parse_log_tail(path: Path, n: int = 80) -> str:
    if not path.exists():
        return f"(no log at {path})"
    try:
        lines = path.read_text().splitlines()
        return "\n".join(lines[-n:])
    except Exception as exc:
        return f"(error reading {path}: {exc})"


def _record_pnl_dollars(rec: dict) -> float | None:
    """Compute net dollars P&L for a closed record.

    Per-share gross = entry_credit - close_cost (the close credit
    in our convention is negative since we pay to buy back).  Net
    after slippage + commission uses the conservative cost model
    we used in backtest: $0.25/leg/side slippage + $1.50/leg
    round-trip commission = $8 per IC.
    """
    if not rec.get("closed"):
        return None
    legs = rec.get("legs", [])
    close_legs = rec.get("close_legs", [])
    if not legs or not close_legs:
        return None
    entry_credit = sum(-leg["quantity"] * leg["entry_mid"] for leg in legs)
    # close_credit is sum(-flipped_qty * close_mid) — sign already flipped
    # in mark_entry_closed.  PnL per share = entry_credit + close_credit.
    close_credit = rec.get("close_credit_per_share", 0.0)
    pnl_per_share_gross = entry_credit + close_credit
    n_legs = len(legs)
    contracts = rec.get("contracts", 1)
    slippage = 2 * n_legs * 0.25 * contracts
    commission = n_legs * 1.50 * contracts
    pnl_dollars_gross = pnl_per_share_gross * MULTIPLIER * contracts
    return pnl_dollars_gross - slippage - commission


def records_to_dataframe(records: list[dict]) -> pd.DataFrame:
    """Tabular view: one row per session, with the fields the
    dashboard wants to show.
    """
    rows = []
    for rec in records:
        legs = rec.get("legs", [])
        if not legs:
            continue
        long_put = next(
            (l for l in legs if l["side"] == "put" and l["quantity"] == +1), None
        )
        short_put = next(
            (l for l in legs if l["side"] == "put" and l["quantity"] == -1), None
        )
        short_call = next(
            (l for l in legs if l["side"] == "call" and l["quantity"] == -1), None
        )
        long_call = next(
            (l for l in legs if l["side"] == "call" and l["quantity"] == +1), None
        )
        entry_credit = sum(-l["quantity"] * l["entry_mid"] for l in legs)
        rows.append({
            "session_date": rec["session_date"],
            "entry_ts": rec.get("ts_utc", "")[:19],
            "underlying_at_entry": rec.get("underlying_at_entry", 0.0),
            "long_put": long_put["strike"] if long_put else None,
            "short_put": short_put["strike"] if short_put else None,
            "short_call": short_call["strike"] if short_call else None,
            "long_call": long_call["strike"] if long_call else None,
            "entry_credit": entry_credit * MULTIPLIER * rec.get("contracts", 1),
            "closed": rec.get("closed", False),
            "close_reason": rec.get("close_reason", "open"),
            "close_ts": (rec.get("close_ts") or "")[:19],
            "pnl_dollars": _record_pnl_dollars(rec),
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df["session_date"] = pd.to_datetime(df["session_date"])
        df = df.sort_values("session_date")
    return df


# ── Sidebar / kill-switch control ──────────────────────────────


with st.sidebar:
    st.title("MES 0DTE Live Paper")
    st.caption(f"Auto-refresh every {REFRESH_SECONDS}s")
    st.caption(f"Last loaded: {datetime.now().strftime('%H:%M:%S')}")

    st.divider()
    st.subheader("Kill switch")
    if KILL_SWITCH_FILE.exists():
        st.error("**ACTIVE** — daemon will refuse new entries and "
                 "force-close any open position on next management run.")
        if st.button("Re-arm daemon (delete kill file)", type="primary"):
            KILL_SWITCH_FILE.unlink()
            st.rerun()
    else:
        st.success("Daemon armed (no kill file)")
        if st.button("Halt daemon (create kill file)", type="secondary"):
            KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
            KILL_SWITCH_FILE.touch()
            st.rerun()

    st.divider()
    st.markdown(
        "**Spec**: IC $10/$10 + 75% PT, 10:00 ET entry, no vol gate"
    )


# ── Main page ──────────────────────────────────────────────────


records = load_entry_records()
df = records_to_dataframe(records)

st.title("📈 MES 0DTE Live Paper Dashboard")

# Today's status
today = date.today()
today_str = today.isoformat()
today_record = next(
    (r for r in records if r.get("session_date") == today_str), None
)

st.subheader("Today")
if today_record is None:
    st.info(
        "**No position today yet.**  "
        "Either the daemon hasn't fired yet (entry runs at 09:55 ET "
        "Mon-Thu) or today is a non-trading day."
    )
else:
    cols = st.columns(5)
    cols[0].metric("Underlying at entry", f"${today_record['underlying_at_entry']:.2f}")
    legs = today_record["legs"]
    short_put_K = next(l["strike"] for l in legs if l["side"] == "put" and l["quantity"] == -1)
    short_call_K = next(l["strike"] for l in legs if l["side"] == "call" and l["quantity"] == -1)
    cols[1].metric("Short put", f"K={short_put_K:.0f}")
    cols[2].metric("Short call", f"K={short_call_K:.0f}")
    entry_credit = sum(-l["quantity"] * l["entry_mid"] for l in legs)
    cols[3].metric("Entry credit ($)", f"${entry_credit * MULTIPLIER:.2f}")
    if today_record.get("closed"):
        pnl = _record_pnl_dollars(today_record)
        cols[4].metric(
            f"Closed ({today_record['close_reason']})",
            f"${pnl:+.2f}" if pnl is not None else "n/a",
            delta=f"{pnl:+.2f}" if pnl is not None else None,
        )
    else:
        cols[4].metric("Status", "OPEN")


# Lifetime stats
st.subheader("Lifetime stats")
closed_df = df[df["closed"]] if not df.empty else df
total_trades = len(closed_df)
if total_trades == 0:
    st.info("No closed trades yet.")
else:
    wins = (closed_df["pnl_dollars"] > 0).sum()
    losses = (closed_df["pnl_dollars"] < 0).sum()
    win_rate = wins / total_trades if total_trades else 0
    total_net = closed_df["pnl_dollars"].sum()
    avg_net = closed_df["pnl_dollars"].mean()
    best = closed_df["pnl_dollars"].max()
    worst = closed_df["pnl_dollars"].min()

    cols = st.columns(6)
    cols[0].metric("Trades", f"{total_trades}")
    cols[1].metric("Win rate", f"{win_rate:.1%}")
    cols[2].metric("Total net", f"${total_net:+.2f}")
    cols[3].metric("Avg / trade", f"${avg_net:+.2f}")
    cols[4].metric("Best", f"${best:+.2f}")
    cols[5].metric("Worst", f"${worst:+.2f}")

    # Cumulative P&L chart.
    chart_df = closed_df.sort_values("session_date").copy()
    chart_df["cumulative_net"] = chart_df["pnl_dollars"].cumsum()
    chart = (
        alt.Chart(chart_df)
        .mark_line(point=True)
        .encode(
            x=alt.X("session_date:T", title="Session date"),
            y=alt.Y("cumulative_net:Q", title="Cumulative net P&L ($)"),
            tooltip=[
                "session_date:T",
                alt.Tooltip("pnl_dollars:Q", title="Trade P&L", format="$.2f"),
                alt.Tooltip("cumulative_net:Q", title="Cumulative", format="$.2f"),
                "close_reason:N",
            ],
        )
        .properties(height=300)
    )
    st.altair_chart(chart, use_container_width=True)


# Trade table
st.subheader("Trade history")
if df.empty:
    st.info("No trades recorded yet.")
else:
    display_df = df.copy().sort_values("session_date", ascending=False)
    # Format columns for display.
    display_df["session_date"] = display_df["session_date"].dt.strftime("%Y-%m-%d (%a)")
    display_df["entry_credit"] = display_df["entry_credit"].apply(
        lambda v: f"${v:+.2f}" if pd.notna(v) else ""
    )
    display_df["pnl_dollars"] = display_df["pnl_dollars"].apply(
        lambda v: f"${v:+.2f}" if pd.notna(v) else ""
    )
    display_df = display_df[[
        "session_date", "underlying_at_entry",
        "long_put", "short_put", "short_call", "long_call",
        "entry_credit", "close_reason", "pnl_dollars",
    ]].rename(columns={
        "session_date": "Date",
        "underlying_at_entry": "MES @ entry",
        "long_put": "Long put",
        "short_put": "Short put",
        "short_call": "Short call",
        "long_call": "Long call",
        "entry_credit": "Credit",
        "close_reason": "Close",
        "pnl_dollars": "Net P&L",
    })
    st.dataframe(display_df, use_container_width=True, hide_index=True)


# Recent log tails
st.subheader("Recent daemon logs")
log_cols = st.columns(2)
with log_cols[0]:
    st.caption(f"Entry log — {today_str}")
    log_path = LOG_DIR / f"{today_str}_entry.log"
    st.code(_parse_log_tail(log_path, n=40), language="text", height=300)
with log_cols[1]:
    st.caption(f"Management log — {today_str}")
    log_path = LOG_DIR / f"{today_str}_manage.log"
    st.code(_parse_log_tail(log_path, n=40), language="text", height=300)


# Auto-rerun timer.
import time as _time
_time.sleep(REFRESH_SECONDS)
st.rerun()
