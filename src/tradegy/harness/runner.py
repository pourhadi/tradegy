"""Backtest harness runner — single spec, single window, session-aware.

Per 05_backtest_harness.md, the runner is the deterministic bar-by-bar
driver that:
  1. Loads + validates the spec (already done by tradegy.specs.loader).
  2. Builds the bar + feature panel for the spec's instrument and window.
  3. Initializes the strategy class state machine for the first session.
  4. For each bar:
     a. If the bar belongs to a different session than the previous bar:
        force-flatten any open position (ExitReason.SESSION_END at the
        prior bar's close), reinitialize strategy state for the new
        session — counters like max_attempts_per_session reset.
     b. Check if a stop was triggered during the bar; if so, emit the
        closing fill at the stop price and record the trade.
     c. Check the spec's exit blocks (time_stop, invalidation
        conditions); if they fire, queue a closing market order for the
        next bar's open.
     d. Increment bars_since_entry if in position.
     e. Call strategy.on_bar; for any returned entry order, queue a
        market fill at the next bar's open (subject to slippage).
  5. Close any open position on the final bar.
  6. Aggregate trades into the AggregateStats block and return.

Session boundaries come from the CMES exchange_calendars calendar by
default; override via the ``session_calendar`` argument. Bars that fall
outside any session (deep weekend, holiday) are skipped — they emit no
strategy callbacks but are otherwise non-events.

The MVP supports market entries only. Limit-order fills (and the
"price-must-trade-through" gating) are deferred until a strategy spec
needs them.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import polars as pl

from tradegy.calendar import session_intervals
from tradegy.harness.data import (
    build_feature_panel,
    iter_bars_with_features,
    load_bar_stream,
    required_feature_ids_for_strategy,
)
from tradegy.harness.execution import (
    CostModel,
    apply_fill,
    fill_market_order,
    fill_stop_at_price,
    stop_triggered_during_bar,
)
from tradegy.harness.stats import AggregateStats, aggregate_trades
from tradegy.specs.schema import StrategySpec
from tradegy.strategies.auxiliary import (
    get_condition_evaluator,
    get_exit_class,
    get_sizing_class,
    get_stop_class,
)
from tradegy.strategies.base import get_strategy_class
from tradegy.strategies.types import (
    Bar,
    ExitReason,
    FeatureSnapshot,
    Order,
    OrderType,
    Position,
    Side,
    State,
    Trade,
)


@dataclass
class BacktestResult:
    spec_id: str
    spec_version: str
    bar_feature_id: str
    bar_cadence: str
    coverage_start: datetime
    coverage_end: datetime
    total_bars: int
    sessions_traversed: int = 0
    trades: list[Trade] = field(default_factory=list)
    stats: AggregateStats | None = None


def run_backtest(
    spec: StrategySpec,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    cost: CostModel | None = None,
    feature_root: Path | None = None,
    session_calendar: str = "CMES",
) -> BacktestResult:
    """Run a single-spec backtest and return trades + stats.

    The driver is session-aware: at every session boundary the harness
    flattens any open position and reinitializes strategy state, so
    ``max_attempts_per_session`` counts correctly per real session.
    Override the calendar via ``session_calendar`` (default CMES, the
    CME futures calendar).
    """
    cost = cost or CostModel()

    # Resolve the strategy class and auxiliary classes once.
    strategy_class = get_strategy_class(spec.entry.strategy_class)
    sizing_class = get_sizing_class(spec.sizing.method)
    initial_stop_method = spec.stops.initial_stop["method"]
    stop_class = get_stop_class(initial_stop_method)
    initial_stop_params = {
        k: v for k, v in spec.stops.initial_stop.items() if k != "method"
    }
    sizing_params = spec.sizing.parameters

    # Time stop is an explicit block, not a registered exit class entry.
    time_stop_enabled = (
        spec.stops.time_stop is not None and spec.stops.time_stop.enabled
    )
    time_stop_max_bars = (
        spec.stops.time_stop.max_holding_bars if time_stop_enabled else None
    )

    # Invalidation conditions resolved once.
    invalidations = [
        (
            get_condition_evaluator(c.condition),
            c.parameters,
            c.action,
        )
        for c in spec.exits.invalidation_conditions
    ]

    # Entry gating conditions resolved once. ALL must evaluate True at
    # the candidate entry bar for `strategy_class.on_bar` to be invoked.
    # Empty list => no gate (legacy behavior).
    gates = [
        (get_condition_evaluator(g.condition), g.parameters)
        for g in spec.entry.gating_conditions
    ]

    # Build the panel.
    instrument = spec.market_scope.instrument
    bar_cadence = "1m"  # MVP convention
    bar_feature_id = f"{instrument.lower()}_{bar_cadence}_bars"
    required_features = list(
        required_feature_ids_for_strategy(spec.entry.strategy_class)
    )
    # Gating + invalidation evaluators reference features by id in their
    # parameters; the panel needs those joined in too. Common keys are
    # `feature_id` and `session_position_feature_id`.
    _COND_FEATURE_KEYS = ("feature_id", "session_position_feature_id")
    for g in spec.entry.gating_conditions:
        for k in _COND_FEATURE_KEYS:
            if k in g.parameters:
                fid = g.parameters[k]
                if fid not in required_features:
                    required_features.append(fid)
    for c in spec.exits.invalidation_conditions:
        for k in _COND_FEATURE_KEYS:
            if k in c.parameters:
                fid = c.parameters[k]
                if fid not in required_features:
                    required_features.append(fid)

    bars = load_bar_stream(
        instrument,
        bar_cadence=bar_cadence,
        start=start,
        end=end,
        feature_root=feature_root,
    )
    if bars.height == 0:
        raise ValueError(
            f"no bars available for {bar_feature_id} in window [{start}, {end}]"
        )
    panel = build_feature_panel(bars, required_features, feature_root=feature_root)

    # Tag each bar with its session id via asof-join against the
    # calendar's session intervals. Bars in non-session windows
    # (weekend / holiday gaps) get session_id=null and are skipped.
    win_start = bars.row(0, named=True)["ts_utc"]
    win_end = bars.row(-1, named=True)["ts_utc"]
    sess_intervals = session_intervals(session_calendar, win_start, win_end)
    panel = _attach_session_ids(panel, sess_intervals)

    # Initialize state for the first session. We re-initialize at every
    # subsequent session boundary inside the loop.
    first_ts = win_start
    state = strategy_class.initialize(
        spec.entry.parameters, instrument, first_ts
    )

    trades: list[Trade] = []
    pending_orders: list[Order] = []  # entry orders queued for next-bar-open fill
    pending_close_reason: ExitReason | None = None  # exit queued for next-bar-open fill

    last_bar: Bar | None = None
    bar_count = 0
    current_session_id: str | None = None
    sessions_traversed = 0

    rows = list(iter_bars_with_features(panel, required_features))
    session_ids = panel.get_column("__session_id").to_list()
    for i, (bar, features) in enumerate(rows):
        bar_count += 1
        bar_session_id = session_ids[i]

        # Skip bars in non-session windows entirely.
        if bar_session_id is None:
            last_bar = bar
            continue

        # Session transition: flatten + reinit before handling this bar.
        if current_session_id is None:
            current_session_id = bar_session_id
            sessions_traversed = 1
        elif bar_session_id != current_session_id:
            if not state.position.is_flat and last_bar is not None:
                # Close at the prior bar's close — that's the end-of-
                # session price (no slippage; treat as session-close
                # auction).
                closing_side = state.position.side
                qty = abs(state.position.quantity)
                entry_px = state.position.avg_entry_price
                entry_ts = state.position.entry_ts
                initial_stop = state.position.initial_stop_price
                holding = state.position.bars_since_entry
                close_fill = fill_market_order(
                    Order(
                        side=Side.SHORT if closing_side == Side.LONG else Side.LONG,
                        type=OrderType.MARKET,
                        quantity=qty,
                        tag=f"session_end:{current_session_id}",
                    ),
                    last_bar.close, last_bar.ts_utc, cost,
                )
                realized = apply_fill(state.position, close_fill)
                trades.append(
                    _build_trade(
                        spec, instrument, closing_side, qty,
                        entry_ts, entry_px, initial_stop,
                        last_bar.ts_utc, close_fill.price, realized,
                        open_commission=cost.commission_per_side * qty,
                        close_fill=close_fill,
                        holding_bars=holding,
                        exit_reason=ExitReason.SESSION_END,
                        cost=cost,
                    )
                )
            # Cancel any pending orders / exits at the boundary; the
            # strategy gets a clean state.
            pending_orders = []
            pending_close_reason = None
            state = strategy_class.initialize(
                spec.entry.parameters, instrument, bar.ts_utc
            )
            current_session_id = bar_session_id
            sessions_traversed += 1

        # 1) Fill any orders queued from the previous bar at this bar's open.
        if pending_orders:
            for order in pending_orders:
                if order.type == OrderType.MARKET:
                    fill = fill_market_order(order, bar.open, bar.ts_utc, cost)
                    apply_fill(state.position, fill)
                    if state.position.quantity != 0:
                        # entry — place initial stop
                        stop_px = stop_class.stop_price(
                            initial_stop_params,
                            state.position.side,
                            state.position.avg_entry_price,
                            bar,
                            features,
                        )
                        state.position.initial_stop_price = stop_px
                        state.position.current_stop_price = stop_px
            pending_orders = []

        # 1a) Close from a queued exit at this bar's open.
        if pending_close_reason is not None and not state.position.is_flat:
            closing_side = state.position.side
            qty = abs(state.position.quantity)
            entry_px = state.position.avg_entry_price
            entry_ts = state.position.entry_ts
            initial_stop = state.position.initial_stop_price
            holding = state.position.bars_since_entry
            close_fill = fill_market_order(
                Order(
                    side=Side.SHORT if closing_side == Side.LONG else Side.LONG,
                    type=OrderType.MARKET,
                    quantity=qty,
                    tag=f"exit:{pending_close_reason.value}",
                ),
                bar.open, bar.ts_utc, cost,
            )
            realized = apply_fill(state.position, close_fill)
            trades.append(
                _build_trade(
                    spec, instrument, closing_side, qty,
                    entry_ts, entry_px, initial_stop,
                    bar.ts_utc, close_fill.price, realized,
                    open_commission=cost.commission_per_side * qty,
                    close_fill=close_fill,
                    holding_bars=holding,
                    exit_reason=pending_close_reason,
                    cost=cost,
                )
            )
            pending_close_reason = None

        # 2) Stop check during this bar.
        if not state.position.is_flat and stop_triggered_during_bar(state.position, bar):
            closing_side = state.position.side
            qty = abs(state.position.quantity)
            entry_px = state.position.avg_entry_price
            entry_ts = state.position.entry_ts
            initial_stop = state.position.initial_stop_price
            holding = state.position.bars_since_entry
            stop_fill = fill_stop_at_price(
                closing_side, qty,
                state.position.current_stop_price,
                bar.ts_utc, cost,
            )
            realized = apply_fill(state.position, stop_fill)
            trades.append(
                _build_trade(
                    spec, instrument, closing_side, qty,
                    entry_ts, entry_px, initial_stop,
                    bar.ts_utc, stop_fill.price, realized,
                    open_commission=cost.commission_per_side * qty,
                    close_fill=stop_fill,
                    holding_bars=holding,
                    exit_reason=ExitReason.STOP,
                    cost=cost,
                )
            )

        # 3) If still in position, increment holding bars and evaluate exits.
        if not state.position.is_flat:
            state.position.bars_since_entry += 1

            if time_stop_enabled and state.position.bars_since_entry >= time_stop_max_bars:
                pending_close_reason = ExitReason.TIME

            for ev, params, _action in invalidations:
                if ev.evaluate(params, bar, features, state.position):
                    pending_close_reason = ExitReason.INVALIDATION
                    break

        # 4) Strategy on_bar — entry orders queued for next-bar-open fill.
        #    Gating conditions act as a hard pre-filter: if any returns
        #    False, the strategy doesn't see this bar at all (no entry
        #    attempt counter is incremented).
        if state.position.is_flat and pending_close_reason is None:
            if gates and not all(
                ev.evaluate(params, bar, features, state.position)
                for ev, params in gates
            ):
                last_bar = bar
                continue
            new_orders = strategy_class.on_bar(state, bar, features)
            for o in new_orders:
                # Apply sizing if the strategy emitted quantity 0/1.
                # Strategies emit "intent" quantity; sizing class can scale
                # based on stop distance. For MVP with fixed_contracts the
                # quantity matches contracts.
                stop_px_preview = stop_class.stop_price(
                    initial_stop_params, o.side, bar.close, bar, features,
                )
                qty = sizing_class.size(
                    sizing_params, o.side, bar.close, stop_px_preview, 0.0,
                )
                if qty <= 0:
                    continue
                pending_orders.append(
                    Order(
                        side=o.side,
                        type=o.type,
                        quantity=qty,
                        limit_price=o.limit_price,
                        stop_price=o.stop_price,
                        tag=o.tag,
                    )
                )

        last_bar = bar

    # Final flush: close any open position at last bar's close (no slippage,
    # treat as session end).
    if not state.position.is_flat and last_bar is not None:
        closing_side = state.position.side
        qty = abs(state.position.quantity)
        entry_px = state.position.avg_entry_price
        entry_ts = state.position.entry_ts
        initial_stop = state.position.initial_stop_price
        holding = state.position.bars_since_entry
        close_fill = fill_market_order(
            Order(
                side=Side.SHORT if closing_side == Side.LONG else Side.LONG,
                type=OrderType.MARKET,
                quantity=qty,
                tag="end_of_window",
            ),
            last_bar.close, last_bar.ts_utc, cost,
        )
        realized = apply_fill(state.position, close_fill)
        trades.append(
            _build_trade(
                spec, instrument, closing_side, qty,
                entry_ts, entry_px, initial_stop,
                last_bar.ts_utc, close_fill.price, realized,
                open_commission=cost.commission_per_side * qty,
                close_fill=close_fill,
                holding_bars=holding,
                exit_reason=ExitReason.SESSION_END,
                cost=cost,
            )
        )

    stats = aggregate_trades(trades)

    return BacktestResult(
        spec_id=spec.metadata.id,
        spec_version=spec.metadata.version,
        bar_feature_id=bar_feature_id,
        bar_cadence=bar_cadence,
        coverage_start=rows[0][0].ts_utc if rows else first_ts,
        coverage_end=rows[-1][0].ts_utc if rows else first_ts,
        total_bars=bar_count,
        sessions_traversed=sessions_traversed,
        trades=trades,
        stats=stats,
    )


def _attach_session_ids(
    panel: pl.DataFrame,
    intervals: list[tuple[str, datetime, datetime]],
) -> pl.DataFrame:
    """Tag each bar with the session id whose [open, close) contains it.

    Uses join_asof on session open + a post-join close-bound check —
    bars in non-session windows end up with __session_id = null.
    """
    if not intervals:
        return panel.with_columns(
            pl.lit(None, dtype=pl.Utf8).alias("__session_id")
        )
    sess_df = pl.DataFrame(
        {
            "__session_id": [iv[0] for iv in intervals],
            "__sess_open": [iv[1] for iv in intervals],
            "__sess_close": [iv[2] for iv in intervals],
        },
        schema={
            "__session_id": pl.Utf8,
            "__sess_open": pl.Datetime("ns", "UTC"),
            "__sess_close": pl.Datetime("ns", "UTC"),
        },
    ).sort("__sess_open")
    return (
        panel.sort("ts_utc")
        .with_columns(pl.col("ts_utc").cast(pl.Datetime("ns", "UTC")))
        .join_asof(
            sess_df,
            left_on="ts_utc",
            right_on="__sess_open",
            strategy="backward",
        )
        .with_columns(
            pl.when(
                pl.col("__sess_close").is_not_null()
                & (pl.col("ts_utc") < pl.col("__sess_close"))
            )
            .then(pl.col("__session_id"))
            .otherwise(None)
            .alias("__session_id")
        )
        .drop(["__sess_open", "__sess_close"])
    )


def _build_trade(
    spec: StrategySpec,
    instrument: str,
    closing_side: Side,
    qty: int,
    entry_ts: datetime,
    entry_px: float,
    initial_stop: float | None,
    exit_ts: datetime,
    exit_px: float,
    realized_pnl: float,
    *,
    open_commission: float,
    close_fill,
    holding_bars: int,
    exit_reason: ExitReason,
    cost: CostModel,
) -> Trade:
    initial_risk_ticks = (
        abs(entry_px - initial_stop) / cost.tick_size if initial_stop else 0.0
    )
    initial_risk_per_contract = (
        abs(entry_px - initial_stop) if initial_stop else 0.0
    )
    commissions = open_commission + close_fill.commission
    slippage_cost = (
        2 * cost.slippage_ticks_per_side * cost.tick_size * qty
    )
    gross_pnl = realized_pnl
    net_pnl = gross_pnl - commissions
    if initial_risk_per_contract > 0:
        net_pnl_R = net_pnl / (initial_risk_per_contract * qty)
    else:
        net_pnl_R = 0.0
    return Trade(
        trade_id=str(uuid.uuid4()),
        strategy_id=spec.metadata.id,
        instrument=instrument,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        side=closing_side,
        quantity=qty,
        entry_price=entry_px,
        exit_price=exit_px,
        initial_stop_price=initial_stop or 0.0,
        initial_risk_ticks=initial_risk_ticks,
        gross_pnl=gross_pnl,
        commissions=commissions,
        slippage_cost=slippage_cost,
        net_pnl=net_pnl,
        net_pnl_R=net_pnl_R,
        holding_bars=holding_bars,
        exit_reason=exit_reason,
    )
