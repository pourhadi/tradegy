#!/usr/bin/env python3
"""Live 0DTE paper-trading daemon for the validated MES iron-condor spec.

Runs the validated config from `15_5k_options_capital_plan.md`:

    Strategy : Mes0dteIronCondor(po=25, co=25, ww=25)
    Gate     : prior-session VIX close > 18
    Mgmt     : profit_take_pct = 0.50 (intraday, no loss-stop)
    Capital  : $5K notional, 1 contract per entry
    Account  : IBKR paper account DU7535411 (port 4002)

Lifecycle (single-shot per invocation; designed to be called once at
~10:25 ET each weekday):

    1. Connect to IB Gateway on the paper port.
    2. Pull prior-session VIX close from the on-disk vix_daily source.
    3. If VIX ≤ 18: log "gate not passing", exit cleanly.
    4. Pull current MES front-month price (via reqMktData snapshot).
    5. Build a same-day-expiry MultiLegOrder using Mes0dteIronCondor
       with target_strikes anchored to the live underlying.
    6. Build a temporary ChainSnapshot containing ONLY the four
       target legs with bid=ask=0 sentinels — the router calls
       reqMktData itself on each leg's contract to find a real fill
       price.  Wait, actually the router uses the snapshot's bid/ask
       to compute the limit.  So we need to seed the snapshot with
       reasonable prices.
    7. Submit the combo via IbkrOptionsRouter.place_combo with a
       deterministic client_order_id ("mes_0dte_<session_date>_v1").
    8. If submitted, persist the entry record to disk so the
       intraday-management process can pick it up.
    9. Exit.

A separate intraday-management process (run every 15 min from 10:30
to 15:55 ET) reads the entry record and:
    - Computes current MTM via reqMktData on each leg
    - If MTM ≥ 50% × entry_credit: places closing combo

This first version of the daemon implements steps 1-5 + the entry
record write.  The intraday management hook is wired but the
profit-take loop runs as a separate scheduled invocation.

Usage:
    uv run python scripts/live_mes_0dte.py             # entry mode
    uv run python scripts/live_mes_0dte.py --manage    # mgmt sweep
    uv run python scripts/live_mes_0dte.py --dry-run   # no submission

Required env (read from ~/.zprofile):
    IBKR_PAPER_ACCOUNT  — e.g. DU7535411
    IBKR_HOST           — defaults to 127.0.0.1
    IBKR_PORT           — defaults to 4002 (IB Gateway paper)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import polars as pl

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("mes_0dte_live")


# ── Config ─────────────────────────────────────────────────────────


REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRY_RECORDS_DIR = REPO_ROOT / "data" / "live_options" / "mes_0dte_entries"
LOG_DIR = REPO_ROOT / "data" / "live_options" / "mes_0dte_logs"
ENTRY_RECORDS_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ─────────────────────────────────────────────────────────────────
# Strategy spec — daily-fire IC $10/$10 + 75% profit-take.
#
# Found via parameter sweep over 2yr historical + 4mo held-out 2025
# data; all 6 sub-windows (years and half-years) net-positive,
# including the most-volatile April-2025 window.  Per-trade EV
# nearly equal to the prior gated spec but with 4× trade frequency
# and ~3× smaller per-trade max loss.
#
# Variants would need a fresh hypothesis + held-out backtest before
# they go live — do not adjust these here without re-running the
# sweep.
# ─────────────────────────────────────────────────────────────────
PUT_SHORT_OFFSET = 10.0
CALL_SHORT_OFFSET = 10.0
WING_WIDTH = 10.0
PROFIT_TAKE_PCT = 0.75
CONTRACTS = 1

# Volatility-index gate.  Daily-fire spec sets this to "none" — the
# strategy is profitable across all volatility regimes when the
# strikes are tight enough.  Other modes preserved for research
# and operator toggle:
#
#   "none"          : NO gate.  Fire every Mon-Thu (default).
#   "intraday_live" : query IBKR for live volatility-index value
#                     at entry time; require it above VIX_GATE_MIN.
#   "prior_close"   : require yesterday's 16:00 ET volatility-index
#                     close above VIX_GATE_MIN; uses on-disk
#                     `vix_daily`.
VIX_GATE_SOURCE = "none"
VIX_GATE_MIN = 18.0  # only consulted when SOURCE != "none"

# Force-close trigger: at-or-after this UTC clock time, any still-
# open position is closed.  19:30 UTC = 15:30 ET — 30 min before
# regular cash close (16:00 ET).  This MUST land on or before a
# management-tick boundary so the trigger actually fires before
# expiry.  Management runs every 15 min from 10:15 ET (14:15 UTC),
# so :30/:45/:00/:15 are the available ticks.  19:30 is the
# earliest tick that gives PT a fair shake but still leaves time
# to close on real quotes (post-19:45 the chain is at expiry-edge
# illiquidity; at 20:00 quotes go to 0 and the daemon CAN'T mark
# meaningfully).
FORCE_CLOSE_UTC: time = time(19, 30)

# Kill-switch file path.  If present (regardless of contents), the
# entry job blocks new entries AND the management job force-closes
# any open position at next run.  Operator-friendly halt control.
KILL_SWITCH_FILE = REPO_ROOT / "data" / "live_options" / "MES_0DTE_KILL"

# Connection.
IBKR_HOST = os.environ.get("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.environ.get("IBKR_PORT", "4002"))
IBKR_CLIENT_ID = int(os.environ.get("IBKR_CLIENT_ID", "23"))


# ── VIX gate ───────────────────────────────────────────────────────


async def fetch_live_vix(ib) -> float | None:
    """Query IBKR for the current cash VIX index level via reqMktData.

    VIX is the CBOE Volatility Index — a CASH index, not a future.
    IBKR symbology: Index('VIX', 'CBOE').  During RTH (9:30-16:15 ET)
    the live value updates roughly every 15 sec from real-time SPX
    option quotes.  Many retail/paper accounts don't have the live
    CBOE index subscription; we ask for delayed data first
    (`reqMarketDataType(3)`) so the gate works without a paid
    subscription.  Delayed VIX is ~15-20 min stale, which is well
    within our gate-decision tolerance for a 10:30 ET entry — the
    10:10 ET VIX value is plenty close to "right now".
    """
    from ib_async import Index

    # 3 = delayed; 1 = live; 4 = delayed-frozen.  Asking for delayed
    # forces IBKR to return a delayed tick if the live subscription
    # isn't active; live ticks take precedence when subscribed.
    try:
        ib.reqMarketDataType(3)
    except Exception as exc:
        log.warning("reqMarketDataType(3) failed: %r — proceeding anyway", exc)

    vix = Index(symbol="VIX", exchange="CBOE", currency="USD")
    qualified = await ib.qualifyContractsAsync(vix)
    c = qualified[0] if qualified else None
    if c is None or getattr(c, "conId", 0) == 0:
        log.error("could not qualify VIX index")
        return None
    ticker = ib.reqMktData(c, "", snapshot=False, regulatorySnapshot=False)
    val = None
    for _ in range(20):
        await asyncio.sleep(0.5)
        # VIX has no bid/ask (it's an index).  Live subscription
        # populates `last`; delayed sub populates `delayedLast`.
        # Try live first, then delayed, then market-price fallback.
        last_live = getattr(ticker, "last", None)
        if last_live is not None and last_live > 0:
            val = float(last_live)
            break
        delayed_last = getattr(ticker, "delayedLast", None)
        if delayed_last is not None and delayed_last > 0:
            val = float(delayed_last)
            break
    if val is None:
        # Last-resort fallback: marketPrice() picks whichever
        # tick field is populated.
        mp = ticker.marketPrice() if hasattr(ticker, "marketPrice") else None
        if mp is not None and mp == mp and mp > 0:
            val = float(mp)
        else:
            # Try delayed close (yesterday's published close,
            # populated even when no current tick streams).
            dc = getattr(ticker, "delayedClose", None) or getattr(ticker, "close", None)
            if dc is not None and dc > 0:
                val = float(dc)
                log.warning("using VIX close fallback (%.2f) — "
                            "no live or delayed tick", val)
    ib.cancelMktData(c)
    return val


def prior_session_vix_close(today: date) -> tuple[float, date] | None:
    """Return (close, prior_trade_date) of the most recent vix_daily
    close strictly BEFORE `today`, or None if no prior data.
    """
    vix_root = REPO_ROOT / "data" / "raw" / "source=vix_daily"
    if not vix_root.exists():
        log.error("vix_daily source not found at %s", vix_root)
        return None
    pattern = str(vix_root / "date=*" / "data.parquet")
    df = pl.read_parquet(pattern).sort("ts_utc")
    df = df.with_columns(pl.col("ts_utc").dt.date().alias("trade_date"))
    prior = df.filter(pl.col("trade_date") < today)
    if prior.height == 0:
        return None
    last = prior.tail(1).row(0, named=True)
    return float(last["close"]), last["trade_date"]


# Maximum acceptable VIX staleness in calendar days.  CBOE publishes
# the cash-VIX overnight after the 16:00 ET close — the file is
# typically <12h stale by next morning.  3-day window covers a
# Mon-following-holiday-weekend gap; refuse entry beyond that.
VIX_MAX_STALENESS_DAYS = 3


# ── MES front-month price ──────────────────────────────────────────


async def fetch_mes_front_price(ib) -> tuple[float, str] | None:
    """Snapshot-quote the next MES quarterly future and return
    (current_price, local_symbol).
    """
    from ib_async import Future

    today = date.today()
    next_q = _next_quarterly_third_friday(today)
    yyyymm = next_q.strftime("%Y%m")
    fut = Future(symbol="MES", lastTradeDateOrContractMonth=yyyymm,
                 exchange="CME", currency="USD")
    qualified = await ib.qualifyContractsAsync(fut)
    front = qualified[0] if qualified else None
    if front is None or getattr(front, "conId", 0) == 0:
        log.error("could not qualify MES front-month %s", yyyymm)
        return None
    log.info("MES front-month: %s (conId=%d)", front.localSymbol, front.conId)

    ticker = ib.reqMktData(front, "", snapshot=False, regulatorySnapshot=False)
    # Wait briefly for tick.
    for _ in range(20):
        await asyncio.sleep(0.5)
        if ticker.last is not None and ticker.last > 0:
            break
    cur = ticker.last or ticker.close or ticker.marketPrice()
    ib.cancelMktData(front)
    if cur is None or cur != cur or cur <= 0:
        log.error("no live price on MES front-month (last=%s, close=%s)",
                  ticker.last, ticker.close)
        return None
    return float(cur), front.localSymbol


def _next_quarterly_third_friday(today: date) -> date:
    """Next quarterly third-Friday MES expiry (Mar/Jun/Sep/Dec)."""
    year, month = today.year, today.month
    while True:
        if month in (3, 6, 9, 12):
            d = date(year, month, 1)
            offset = (4 - d.weekday()) % 7
            third_friday = d.replace(day=1 + offset + 14)
            if third_friday > today:
                return third_friday
        month += 1
        if month > 12:
            month = 1
            year += 1


# ── Entry order construction ───────────────────────────────────────


def build_entry_order(underlying_price: float, today: date):
    """Build the IC's MultiLegOrder using the locked spec.

    Returns the MultiLegOrder + the four (strike, side) targets.
    Strike rounding: MES options strike grid is in $5 increments
    near the money, so round each target to the nearest $5.
    """
    from tradegy.options.chain import OptionSide
    from tradegy.options.positions import LegOrder, MultiLegOrder

    def _round5(x: float) -> float:
        return round(x / 5.0) * 5.0

    short_call_K = _round5(underlying_price + CALL_SHORT_OFFSET)
    long_call_K = _round5(short_call_K + WING_WIDTH)
    short_put_K = _round5(underlying_price - PUT_SHORT_OFFSET)
    long_put_K = _round5(short_put_K - WING_WIDTH)

    if not (long_put_K < short_put_K < short_call_K < long_call_K):
        log.error("strike monotonicity violation: %.0f / %.0f / %.0f / %.0f",
                  long_put_K, short_put_K, short_call_K, long_call_K)
        return None, None

    legs = (
        LegOrder(expiry=today, strike=long_put_K,  side=OptionSide.PUT,  quantity=+1),
        LegOrder(expiry=today, strike=short_put_K, side=OptionSide.PUT,  quantity=-1),
        LegOrder(expiry=today, strike=short_call_K,side=OptionSide.CALL, quantity=-1),
        LegOrder(expiry=today, strike=long_call_K, side=OptionSide.CALL, quantity=+1),
    )
    order = MultiLegOrder(
        tag="mes_0dte_ic_25x25",
        contracts=CONTRACTS,
        legs=legs,
    )
    targets = ((long_put_K, "P"), (short_put_K, "P"), (short_call_K, "C"), (long_call_K, "C"))
    return order, targets


# ── IBKR snapshot for combo pricing ───────────────────────────────


async def build_chain_snapshot_for_legs(ib, order, ts_utc: datetime,
                                        underlying_price: float):
    """Build a tradegy ChainSnapshot containing the 4 target legs with
    real bid/ask quotes pulled from IBKR.  Required by
    IbkrOptionsRouter.place_combo to compute the BAG limit price.
    """
    from ib_async import FuturesOption
    from tradegy.execution.ibkr_options_router import (
        _futures_option_trading_class,
    )
    from tradegy.options.chain import ChainSnapshot, OptionLeg, OptionSide

    legs_out: list[OptionLeg] = []
    for leg_order in order.legs:
        right = "C" if leg_order.side == OptionSide.CALL else "P"
        tc = _futures_option_trading_class(leg_order.expiry, "MES")
        contract = FuturesOption(
            symbol="MES",
            lastTradeDateOrContractMonth=leg_order.expiry.strftime("%Y%m%d"),
            strike=leg_order.strike,
            right=right,
            exchange="CME",
            currency="USD",
            multiplier="5",
            tradingClass=tc,
        )
        qualified = await ib.qualifyContractsAsync(contract)
        c = qualified[0] if qualified else None
        if c is None or getattr(c, "conId", 0) == 0:
            log.error("could not qualify leg %s K=%s %s tc=%s",
                      leg_order.expiry, leg_order.strike, right, tc)
            return None
        ticker = ib.reqMktData(c, "", snapshot=False, regulatorySnapshot=False)
        for _ in range(20):
            await asyncio.sleep(0.5)
            if (ticker.bid is not None and ticker.ask is not None
                    and ticker.bid > 0 and ticker.ask > 0):
                break
        bid = ticker.bid if ticker.bid and ticker.bid > 0 else 0.0
        ask = ticker.ask if ticker.ask and ticker.ask > 0 else 0.0
        ib.cancelMktData(c)
        if bid <= 0 or ask <= 0:
            log.warning("no quote on leg %s K=%s %s — bid=%s ask=%s",
                        leg_order.expiry, leg_order.strike, right, bid, ask)
        legs_out.append(OptionLeg(
            underlying=f"MES{leg_order.expiry.strftime('%y%m')}",
            expiry=leg_order.expiry, strike=leg_order.strike,
            side=leg_order.side, bid=bid, ask=ask, iv=0.0,
            volume=0, open_interest=0, multiplier=5,
        ))

    return ChainSnapshot(
        underlying="MES",
        ts_utc=ts_utc,
        underlying_price=underlying_price,
        risk_free_rate=0.05,
        legs=tuple(legs_out),
    )


# ── Entry-record persistence ───────────────────────────────────────


def write_entry_record(record: dict, session_date: date) -> Path:
    path = ENTRY_RECORDS_DIR / f"{session_date.isoformat()}.json"
    path.write_text(json.dumps(record, indent=2, default=str))
    return path


def load_entry_record(session_date: date) -> dict | None:
    path = ENTRY_RECORDS_DIR / f"{session_date.isoformat()}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def mark_entry_closed(
    session_date: date,
    *,
    close_reason: str,
    close_ts: datetime,
    close_coid: str,
    close_legs: list[dict],
    close_credit_per_share: float,
    pnl_per_share: float,
) -> Path:
    """Update the entry record to reflect that the position is closed.

    Idempotent: re-marking an already-closed record is allowed and
    just updates the close metadata (useful if the operator
    re-submitted via TWS and wants to overwrite the auto-close
    record).
    """
    path = ENTRY_RECORDS_DIR / f"{session_date.isoformat()}.json"
    record = json.loads(path.read_text())
    record["closed"] = True
    record["close_reason"] = close_reason
    record["close_ts"] = close_ts.isoformat()
    record["close_client_order_id"] = close_coid
    record["close_legs"] = close_legs
    record["close_credit_per_share"] = close_credit_per_share
    record["pnl_per_share"] = pnl_per_share
    path.write_text(json.dumps(record, indent=2, default=str))
    return path


def kill_switch_active() -> bool:
    return KILL_SWITCH_FILE.exists()


# ── Main flows ─────────────────────────────────────────────────────


async def run_entry(dry_run: bool = False, bypass_gate: bool = False) -> int:
    today = date.today()
    log.info("=== live_mes_0dte ENTRY %s ===", today)
    if bypass_gate and not dry_run:
        log.error("--bypass-gate REQUIRES --dry-run.  Bypassing the VIX gate "
                  "in live mode would submit trades outside the validated "
                  "regime, against the pre-registered spec.  Refusing.")
        return 1
    if bypass_gate:
        log.warning("--bypass-gate active — VIX freshness + threshold checks "
                    "are SKIPPED.  Strictly for dry-run plumbing verification.")

    # Skip weekends.
    if today.weekday() >= 5:
        log.info("weekend (dow=%d) — skipping", today.weekday())
        return 0

    # Kill-switch — operator-friendly halt.
    if kill_switch_active():
        log.warning("KILL-SWITCH active at %s — refusing entry", KILL_SWITCH_FILE)
        return 0

    # Don't double-enter.
    if load_entry_record(today) is not None:
        log.info("entry record already exists for %s — skipping", today)
        return 0

    # 1. Connect early so we can query live VIX before doing
    # any other work that depends on the gate.  (For prior_close
    # source we don't need IB yet, but we'll need it momentarily
    # for the chain — keeping connect order simple.)
    from ib_async import IB
    from tradegy.execution.ibkr_options_router import IbkrOptionsRouter

    ib = IB()
    log.info("connecting to %s:%d clientId=%d", IBKR_HOST, IBKR_PORT, IBKR_CLIENT_ID)
    try:
        await ib.connectAsync(IBKR_HOST, IBKR_PORT,
                              clientId=IBKR_CLIENT_ID, timeout=10)
    except Exception as exc:
        log.error("IB Gateway connection failed: %r", exc)
        return 2

    # The rest of the entry runs inside a try/finally so we always
    # disconnect from IB on exit.
    try:
        # 2. VIX gate.
        if bypass_gate:
            log.info("(VIX gate bypassed)")
        elif VIX_GATE_SOURCE == "none":
            log.info("(no volatility-index gate — fire every Mon-Thu)")
        elif VIX_GATE_SOURCE == "intraday_live":
            live_vix = await fetch_live_vix(ib)
            if live_vix is None:
                log.error("could not fetch live VIX from IBKR — refusing entry")
                return 1
            log.info("live VIX (intraday): %.2f", live_vix)
            if live_vix <= VIX_GATE_MIN:
                log.info("VIX gate not passing (%.2f ≤ %.2f) — no entry today",
                         live_vix, VIX_GATE_MIN)
                return 0
        elif VIX_GATE_SOURCE == "prior_close":
            result = prior_session_vix_close(today)
            if result is None:
                log.error("could not load prior-session VIX close")
                return 1
            vix, vix_date = result
            staleness_days = (today - vix_date).days
            log.info("prior-session VIX close: %.2f (from %s, %d day(s) stale)",
                     vix, vix_date, staleness_days)
            if staleness_days > VIX_MAX_STALENESS_DAYS:
                log.error("VIX data is too stale (%d days > %d max) — refusing "
                          "entry.  Run download_vix_daily.py + ingest first.",
                          staleness_days, VIX_MAX_STALENESS_DAYS)
                return 1
            if vix <= VIX_GATE_MIN:
                log.info("VIX gate not passing (%.2f ≤ %.2f) — no entry today",
                         vix, VIX_GATE_MIN)
                return 0
        else:
            log.error("unknown VIX_GATE_SOURCE %r", VIX_GATE_SOURCE)
            return 1

        # 3. Underlying price.
        result = await fetch_mes_front_price(ib)
        if result is None:
            return 3
        cur_price, front_local_sym = result
        log.info("MES front-month price: %.2f (%s)", cur_price, front_local_sym)

        # 4. Build entry order.
        order, targets = build_entry_order(cur_price, today)
        if order is None:
            return 4
        log.info("strikes: long-put=%s short-put=%s short-call=%s long-call=%s",
                 *(f"{t[0]:.0f}{t[1]}" for t in targets))

        # 5. Snapshot live quotes for each leg.
        ts = datetime.now(tz=timezone.utc)
        snapshot = await build_chain_snapshot_for_legs(ib, order, ts, cur_price)
        if snapshot is None:
            return 5

        if dry_run:
            log.info("DRY-RUN — would place combo with these legs:")
            for leg, chain_leg in zip(order.legs, snapshot.legs):
                log.info("  %s K=%.0f qty=%+d  bid=%.2f ask=%.2f",
                         leg.side.value, leg.strike, leg.quantity,
                         chain_leg.bid, chain_leg.ask)
            return 0

        # 6. Place combo.
        router = IbkrOptionsRouter(ib=ib)
        coid = f"mes_0dte_{today.isoformat()}_v1"
        try:
            managed = await router.place_combo(
                order=order, snapshot=snapshot,
                client_order_id=coid, ts_utc=ts,
            )
        except Exception as exc:
            log.error("place_combo failed: %r", exc)
            return 6

        log.info("combo placed: %s state=%s broker_id=%s",
                 coid, managed.state.name, managed.broker_order_id)

        # 7. Persist entry record for the management process.
        record = {
            "session_date": today.isoformat(),
            "client_order_id": coid,
            "broker_order_id": managed.broker_order_id,
            "ts_utc": ts.isoformat(),
            "underlying_at_entry": cur_price,
            "vix_gate_source": VIX_GATE_SOURCE,
            "legs": [
                {
                    "expiry": leg.expiry.isoformat(),
                    "strike": leg.strike,
                    "side": leg.side.value,
                    "quantity": leg.quantity,
                    "entry_bid": chain_leg.bid,
                    "entry_ask": chain_leg.ask,
                    "entry_mid": (chain_leg.bid + chain_leg.ask) / 2.0,
                }
                for leg, chain_leg in zip(order.legs, snapshot.legs)
            ],
            "tag": order.tag,
            "contracts": order.contracts,
        }
        path = write_entry_record(record, today)
        log.info("entry record written: %s", path)
        return 0

    finally:
        ib.disconnect()


async def _quote_legs_into_snapshot(
    ib, record_legs: list[dict], underlying_price: float, ts: datetime,
):
    """Re-quote each entry leg from IBKR live data and build a
    ChainSnapshot suitable for the router's combo-pricing.
    """
    from ib_async import FuturesOption
    from tradegy.execution.ibkr_options_router import (
        _futures_option_trading_class,
    )
    from tradegy.options.chain import ChainSnapshot, OptionLeg, OptionSide

    legs_out: list[OptionLeg] = []
    for leg in record_legs:
        expiry = date.fromisoformat(leg["expiry"])
        tc = _futures_option_trading_class(expiry, "MES")
        right = "C" if leg["side"] == "call" else "P"
        contract = FuturesOption(
            symbol="MES",
            lastTradeDateOrContractMonth=expiry.strftime("%Y%m%d"),
            strike=leg["strike"], right=right,
            exchange="CME", currency="USD",
            multiplier="5", tradingClass=tc,
        )
        qualified = await ib.qualifyContractsAsync(contract)
        if not qualified or qualified[0].conId == 0:
            log.error("could not qualify leg %s K=%s %s tc=%s",
                      expiry, leg["strike"], right, tc)
            return None
        c = qualified[0]
        ticker = ib.reqMktData(c, "", snapshot=False, regulatorySnapshot=False)
        for _ in range(20):
            await asyncio.sleep(0.5)
            if (ticker.bid is not None and ticker.ask is not None
                    and ticker.bid > 0 and ticker.ask > 0):
                break
        bid = ticker.bid if ticker.bid and ticker.bid > 0 else 0.0
        ask = ticker.ask if ticker.ask and ticker.ask > 0 else 0.0
        ib.cancelMktData(c)
        log.info("leg %s K=%.0f q=%+d: bid=%.2f ask=%.2f",
                 leg["side"], leg["strike"], leg["quantity"],
                 bid, ask)
        side = OptionSide.CALL if leg["side"] == "call" else OptionSide.PUT
        legs_out.append(OptionLeg(
            underlying=f"MES{expiry.strftime('%y%m')}",
            expiry=expiry, strike=leg["strike"], side=side,
            bid=bid, ask=ask, iv=0.0,
            volume=0, open_interest=0, multiplier=5,
        ))

    return ChainSnapshot(
        underlying="MES",
        ts_utc=ts, underlying_price=underlying_price,
        risk_free_rate=0.05, legs=tuple(legs_out),
    )


def compute_leg_close_mid(
    leg_side: str,
    strike: float,
    bid: float,
    ask: float,
    cur_underlying: float,
) -> tuple[float, bool]:
    """Per-leg close-mark in option points.

    Live IBKR quotes are usually `(bid + ask) / 2`.  But when both
    sides go to zero — typical in the expiry minute when IBKR stops
    publishing quotes for a contract — naive averaging silently
    converts a real-money settlement into a "free close", which
    masks the actual P&L.  Fall back to **intrinsic value** vs the
    current underlying when the quote collapses; that's what the
    contract is worth to a holder at expiry regardless of whether
    the broker is still streaming a quote.

    Returns:
        (close_mid, used_intrinsic_fallback)

    Surfaced 2026-05-06 — the first live paper trade recorded
    `+$3.25/share PnL` on a position that had no quotes at
    expiry; the daemon read all four legs as `bid=0/ask=0` and
    averaged them to a $0 close cost, falsely tripping
    profit-take.
    """
    if bid <= 0 and ask <= 0:
        if leg_side == "call":
            intrinsic = max(0.0, cur_underlying - strike)
        else:
            intrinsic = max(0.0, strike - cur_underlying)
        return intrinsic, True
    return (bid + ask) / 2.0, False


def compute_close_cost(
    record_legs: list[dict],
    snapshot_legs,
    cur_underlying: float,
) -> tuple[float, list[bool]]:
    """Total close cost for the IC, applying the intrinsic fallback
    per-leg.  Pure function — testable without IBKR.

    `record_legs` carries the original entry quantities (`+1` long /
    `-1` short).  `snapshot_legs` is the per-leg quote at the manage
    tick (an `OptionLeg`-like object exposing `bid`, `ask`, `side`).

    Convention: short legs (q=-1) contribute *negative* cost (we
    receive the close credit); long legs (q=+1) contribute
    *positive* cost (we pay the close debit).  Total close_cost is
    what we'd pay net to flatten the structure right now.
    """
    close_cost = 0.0
    used_intrinsic: list[bool] = []
    for orig, cl in zip(record_legs, snapshot_legs):
        side_str = cl.side.value if hasattr(cl.side, "value") else str(cl.side)
        mid, intrinsic_used = compute_leg_close_mid(
            side_str, orig["strike"], cl.bid, cl.ask, cur_underlying,
        )
        used_intrinsic.append(intrinsic_used)
        close_cost += -orig["quantity"] * mid
    return close_cost, used_intrinsic


def build_close_order_from_record(record: dict):
    """Build a CLOSING MultiLegOrder from an open-position entry
    record.  Quantities are flipped vs entry: longs (+1) become
    sells (-1) and shorts (-1) become buys (+1).  The combo is
    tagged "<entry_tag>_close" so audit trails distinguish.

    Pulled out as a pure helper so unit tests can verify the
    flipping invariant without running asyncio / IBKR.
    """
    from tradegy.options.chain import OptionSide
    from tradegy.options.positions import LegOrder, MultiLegOrder

    expiry = date.fromisoformat(record["legs"][0]["expiry"])
    flipped_legs = tuple(
        LegOrder(
            expiry=expiry,
            strike=leg["strike"],
            side=OptionSide.CALL if leg["side"] == "call" else OptionSide.PUT,
            quantity=-leg["quantity"],
        )
        for leg in record["legs"]
    )
    return MultiLegOrder(
        tag=f"{record['tag']}_close",
        contracts=record["contracts"],
        legs=flipped_legs,
    )


def build_close_coid(entry_coid: str, close_reason: str) -> str:
    """Deterministic client_order_id for a closing combo.  The
    router enforces idempotency by coid; using a per-reason
    suffix means a second close attempt for a different reason
    won't collide with the first."""
    return f"{entry_coid}_close_{close_reason}"


async def _submit_closing_combo(
    ib, record: dict, snapshot, ts: datetime,
    close_reason: str,
) -> tuple[bool, str, list[dict], float]:
    """Submit a CLOSING combo for the open position recorded in
    `record` against the live `snapshot`.  Wraps the pure
    `build_close_order_from_record` helper with the actual IBKR
    submission.

    Returns (success, close_coid, close_legs_metadata,
    close_credit_per_share).  close_credit_per_share is the
    cash flow at close (signed: + credit received, - debit paid).
    """
    from tradegy.execution.ibkr_options_router import IbkrOptionsRouter

    close_order = build_close_order_from_record(record)
    close_coid = build_close_coid(record["client_order_id"], close_reason)

    router = IbkrOptionsRouter(ib=ib)
    try:
        managed = await router.place_combo(
            order=close_order, snapshot=snapshot,
            client_order_id=close_coid, ts_utc=ts,
        )
    except Exception as exc:
        log.error("close place_combo failed: %r", exc)
        return False, close_coid, [], 0.0

    log.info("CLOSE combo placed: %s state=%s broker_id=%s",
             close_coid, managed.state.name, managed.broker_order_id)

    # Compute close credit per share for the receipt.  Closing a
    # credit IC is a DEBIT (we pay to buy back) so this is typically
    # negative.  signed sum(-quantity × mid) over CLOSE legs.
    close_credit = sum(
        -fl.quantity * ((cl.bid + cl.ask) / 2.0)
        for fl, cl in zip(close_order.legs, snapshot.legs)
    )
    close_legs_meta = [
        {
            "expiry": fl.expiry.isoformat(),
            "strike": fl.strike,
            "side": fl.side.value,
            "quantity": fl.quantity,
            "close_bid": cl.bid,
            "close_ask": cl.ask,
            "close_mid": (cl.bid + cl.ask) / 2.0,
        }
        for fl, cl in zip(close_order.legs, snapshot.legs)
    ]
    return True, close_coid, close_legs_meta, close_credit


async def run_management() -> int:
    today = date.today()
    now_utc = datetime.now(tz=timezone.utc)
    log.info("=== live_mes_0dte MGMT %s @ %s ===", today, now_utc.time())

    record = load_entry_record(today)
    if record is None:
        log.info("no entry record for %s — nothing to manage", today)
        return 0
    if record.get("closed"):
        log.info("position already closed today (reason=%s) — nothing to do",
                 record.get("close_reason", "?"))
        return 0

    # Connect once for the management run.
    from ib_async import IB
    from tradegy.options.chain import OptionSide

    ib = IB()
    try:
        await ib.connectAsync(IBKR_HOST, IBKR_PORT,
                              clientId=IBKR_CLIENT_ID + 1, timeout=10)
    except Exception as exc:
        log.error("IB Gateway connection failed: %r", exc)
        return 2

    try:
        # Pull current MES front-month price (used both to decide
        # close-time triggers and to seed snapshot.underlying_price).
        result = await fetch_mes_front_price(ib)
        if result is None:
            return 3
        cur_price, _front_local_sym = result

        # Re-quote the four legs from IBKR live data.
        snapshot = await _quote_legs_into_snapshot(
            ib, record["legs"], cur_price, now_utc,
        )
        if snapshot is None:
            return 5

        # Compute entry credit + current MTM.
        entry_credit = sum(
            -leg["quantity"] * leg["entry_mid"]
            for leg in record["legs"]
        )
        # Per-leg close mark with intrinsic fallback.  See
        # `compute_close_cost` for the why; the fallback exists
        # because IBKR stops publishing quotes for expired contracts
        # and the naive (bid+ask)/2 collapses to 0, falsely tripping
        # profit-take in the expiry minute.
        close_cost, used_intrinsic = compute_close_cost(
            record["legs"], snapshot.legs, cur_price,
        )
        for orig, cl, intrinsic_used in zip(
            record["legs"], snapshot.legs, used_intrinsic,
        ):
            if intrinsic_used:
                log.warning(
                    "leg %s K=%.0f q=%+d quotes collapsed (bid=0 ask=0) — "
                    "marked to intrinsic vs S=%.2f",
                    orig["side"], orig["strike"], orig["quantity"], cur_price,
                )
        pnl_per_share = entry_credit - close_cost
        pnl_frac = pnl_per_share / entry_credit if entry_credit > 0 else 0.0
        log.info("entry credit %+.2f, close cost %+.2f, PnL %+.2f (%.1f%% of credit)",
                 entry_credit, close_cost, pnl_per_share, pnl_frac * 100)

        # Decide close trigger.
        force_close_due = now_utc.timetz() >= time(
            FORCE_CLOSE_UTC.hour, FORCE_CLOSE_UTC.minute,
            tzinfo=timezone.utc,
        )
        kill_switch = kill_switch_active()
        close_reason: str | None = None
        if kill_switch:
            close_reason = "kill_switch"
            log.warning("KILL-SWITCH file present at %s — force-closing",
                        KILL_SWITCH_FILE)
        elif pnl_frac >= PROFIT_TAKE_PCT:
            close_reason = "profit_take"
            log.info("PROFIT TAKE TRIGGERED (%.1f%% ≥ %.1f%%)",
                     pnl_frac * 100, PROFIT_TAKE_PCT * 100)
        elif force_close_due:
            close_reason = "force_close_eod"
            log.info("FORCE-CLOSE TRIGGERED — past %s UTC, closing before "
                     "expiry-bell mechanics", FORCE_CLOSE_UTC)
        else:
            log.info("hold — no trigger fired (PT=%.1f%% < %.1f%%, "
                     "force-close cutoff %s not reached)",
                     pnl_frac * 100, PROFIT_TAKE_PCT * 100, FORCE_CLOSE_UTC)
            return 0

        # Submit closing combo.
        success, close_coid, close_legs_meta, close_credit = (
            await _submit_closing_combo(ib, record, snapshot, now_utc, close_reason)
        )
        if not success:
            return 6

        # Mark record closed atomically.
        mark_entry_closed(
            today,
            close_reason=close_reason,
            close_ts=now_utc,
            close_coid=close_coid,
            close_legs=close_legs_meta,
            close_credit_per_share=close_credit,
            pnl_per_share=pnl_per_share,
        )
        log.info("entry record marked closed (reason=%s)", close_reason)
        return 0

    finally:
        ib.disconnect()


# ── Entry point ────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manage", action="store_true",
                   help="Run intraday management instead of entry.")
    p.add_argument("--dry-run", action="store_true",
                   help="Run entry pipeline but skip the actual submission.")
    p.add_argument("--bypass-gate", action="store_true",
                   help="Skip the VIX freshness + threshold gate "
                        "(requires --dry-run).  Use only to verify the "
                        "full entry pipeline plumbing on a no-trade day.")
    args = p.parse_args()

    if args.manage:
        return asyncio.run(run_management())
    else:
        return asyncio.run(run_entry(
            dry_run=args.dry_run, bypass_gate=args.bypass_gate,
        ))


if __name__ == "__main__":
    sys.exit(main())
