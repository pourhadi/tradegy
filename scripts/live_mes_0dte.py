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

# Strategy spec — locked to the OOS-validated config.  Variants would
# need a fresh hypothesis YAML + walk-forward; do not loosen here.
PUT_SHORT_OFFSET = 25.0
CALL_SHORT_OFFSET = 25.0
WING_WIDTH = 25.0
VIX_GATE_MIN_CLOSE = 18.0
PROFIT_TAKE_PCT = 0.50
CONTRACTS = 1

# Force-close trigger: at this UTC clock time on the entry day, any
# still-open position is closed market-style to avoid expiry-bell
# settlement complications.  19:55 UTC = 15:55 ET = 5 minutes before
# the regular cash close (16:00 ET).
FORCE_CLOSE_UTC: time = time(19, 55)

# Kill-switch file path.  If present (regardless of contents), the
# entry job blocks new entries AND the management job force-closes
# any open position at next run.  Operator-friendly halt control.
KILL_SWITCH_FILE = REPO_ROOT / "data" / "live_options" / "MES_0DTE_KILL"

# Connection.
IBKR_HOST = os.environ.get("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.environ.get("IBKR_PORT", "4002"))
IBKR_CLIENT_ID = int(os.environ.get("IBKR_CLIENT_ID", "23"))


# ── VIX gate ───────────────────────────────────────────────────────


def prior_session_vix_close(today: date) -> float | None:
    """Return the most recent vix_daily close strictly BEFORE `today`."""
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
    return float(prior.tail(1)["close"].item())


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
    if not qualified or qualified[0].conId == 0:
        log.error("could not qualify MES front-month %s", yyyymm)
        return None
    front = qualified[0]
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
        if not qualified or qualified[0].conId == 0:
            log.error("could not qualify leg %s K=%s %s tc=%s",
                      leg_order.expiry, leg_order.strike, right, tc)
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


async def run_entry(dry_run: bool = False) -> int:
    today = date.today()
    log.info("=== live_mes_0dte ENTRY %s ===", today)

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

    # 1. VIX gate.
    vix = prior_session_vix_close(today)
    if vix is None:
        log.error("could not load prior-session VIX close")
        return 1
    log.info("prior-session VIX close: %.2f", vix)
    if vix <= VIX_GATE_MIN_CLOSE:
        log.info("VIX gate not passing (%.2f ≤ %.2f) — no entry today",
                 vix, VIX_GATE_MIN_CLOSE)
        return 0

    # 2. Connect.
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

    try:
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
            "vix_prior_close": vix,
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
        # close_cost = -quantity × current_mid summed over original
        # entry-leg directions (NOT flipped — this is the cost to
        # CLOSE at current quotes given our existing position).
        close_cost = sum(
            -orig["quantity"] * ((cl.bid + cl.ask) / 2.0)
            for orig, cl in zip(record["legs"], snapshot.legs)
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
    args = p.parse_args()

    if args.manage:
        return asyncio.run(run_management())
    else:
        return asyncio.run(run_entry(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
