"""Portfolio runner — multiple OptionStrategy instances in parallel
sharing one capital pool.

Differs from `run_options_backtest` (single-strategy) in that:

  - Multiple strategies are called per snapshot. Each gets the
    SHARED `open_positions` tuple but filters internally to its own
    via `OptionStrategy._my_open`.
  - Each strategy's order is queued INDEPENDENTLY → multiple new
    positions can fill on the same snap, up to the RiskManager's
    capital cap.
  - Per-strategy ManagementRules supported via the `rules_by_id`
    parameter (calendars need debit triggers, condors don't, etc.).
  - The OptionsBacktestResult tracks per-strategy stats AND the
    aggregate portfolio P&L trajectory.

Capital semantics: the RiskManager evaluates each candidate
position against the aggregate open-position pool. So if PCS opens
a $5K-max-loss position and JL wants to open a $4K-max-loss one,
the JL evaluation sees the $5K already deployed and checks the
combined $9K against the cap.

Per `14_options_volatility_selling.md` Phase D-9 (this commit):
diversification at retail-account capital is the highest-leverage
return driver. Single-strategy on $25K SPX produced 25 trades over
6 years with $48K capital-cap rejections; multi-strategy on
$25K-XSP should fit nearly all opportunities and capture
independent regime exposures.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from tradegy.options.chain import ChainSnapshot
from tradegy.options.cost_model import OptionCostModel
from tradegy.options.positions import MultiLegOrder, MultiLegPosition
from tradegy.options.risk import RiskManager
from tradegy.options.runner import (
    ClosedTrade,
    OptionsBacktestResult,
    RejectedOrder,
    SnapshotPnL,
    _close_position,
    _open_position_from_order,
)
from tradegy.options.strategy import (
    ManagementRules,
    OptionStrategy,
    should_close,
)


_log = logging.getLogger(__name__)


@dataclass
class PortfolioBacktestResult:
    """Aggregate + per-strategy result of a portfolio backtest.

    `per_strategy` keys are strategy.id strings; values are
    OptionsBacktestResult instances scoped to that strategy's own
    closed trades. The aggregate fields below sum across all
    strategies.
    """

    n_snapshots_seen: int = 0
    aggregate_closed_trades: list[ClosedTrade] = field(default_factory=list)
    aggregate_rejected_orders: list[RejectedOrder] = field(default_factory=list)
    aggregate_snapshot_pnl: list[SnapshotPnL] = field(default_factory=list)
    per_strategy: dict[str, OptionsBacktestResult] = field(default_factory=dict)

    @property
    def realized_pnl_dollars(self) -> float:
        return sum(t.closed_pnl_dollars for t in self.aggregate_closed_trades)

    @property
    def n_closed_trades(self) -> int:
        return len(self.aggregate_closed_trades)

    @property
    def hit_rate(self) -> float:
        if self.n_closed_trades == 0:
            return float("nan")
        wins = sum(1 for t in self.aggregate_closed_trades if t.closed_pnl_dollars > 0)
        return wins / self.n_closed_trades

    @property
    def max_drawdown_dollars(self) -> float:
        if not self.aggregate_snapshot_pnl:
            return 0.0
        peak = self.aggregate_snapshot_pnl[0].realized_dollars_cumulative
        worst = 0.0
        for snap in self.aggregate_snapshot_pnl:
            cur = snap.realized_dollars_cumulative
            peak = max(peak, cur)
            dd = cur - peak
            if dd < worst:
                worst = dd
        return worst


def run_options_backtest_portfolio(
    *,
    strategies: list[OptionStrategy],
    snapshots: Iterable[ChainSnapshot],
    cost: OptionCostModel | None = None,
    rules: ManagementRules | None = None,
    rules_by_id: dict[str, ManagementRules] | None = None,
    risk: RiskManager | None = None,
) -> PortfolioBacktestResult:
    """Run `strategies` in parallel against `snapshots`.

    Each strategy gets the same `open_positions` view but filters
    via `_my_open` to apply its own concentration rule. Multiple
    new positions can fill on the same snap (limited by the
    RiskManager's capital cap, which sees the aggregate exposure).

    `rules` is the default ManagementRules (applied to credit
    positions). `rules_by_id` overrides per-strategy: e.g.
    `{"put_calendar_30_60_atm_deb": ManagementRules(profit_take_pct
    _of_debit=0.25, ...)}`. A strategy without a per-id rule falls
    back to `rules`.

    Returns PortfolioBacktestResult with aggregate + per-strategy
    breakdown.
    """
    cost = cost or OptionCostModel()
    default_rules = rules or ManagementRules()
    rules_by_id = rules_by_id or {}

    # Per-strategy state
    pending_orders: list[tuple[str, MultiLegOrder, datetime]] = []  # (strategy_id, order, submitted_ts)
    open_positions: list[MultiLegPosition] = []
    next_position_id = 0
    realized_cumulative = 0.0
    snapshot_history: list[ChainSnapshot] = []

    # Per-strategy result accumulators.
    per_strategy: dict[str, OptionsBacktestResult] = {
        s.id: OptionsBacktestResult(strategy_id=s.id, n_snapshots_seen=0)
        for s in strategies
    }
    portfolio = PortfolioBacktestResult(per_strategy=per_strategy)

    for snap in snapshots:
        portfolio.n_snapshots_seen += 1
        snapshot_history.append(snap)
        for r in per_strategy.values():
            r.n_snapshots_seen += 1

        # 1. Mark + management on every open position. Each closes
        #    under the rules attached to its strategy class.
        still_open: list[MultiLegPosition] = []
        for pos in open_positions:
            pos_rules = rules_by_id.get(pos.strategy_class, default_rules)
            reason = should_close(pos, snap, pos_rules)
            if reason is None:
                still_open.append(pos)
                continue
            closed = _close_position(pos, snap, cost, reason)
            portfolio.aggregate_closed_trades.append(closed)
            per_strategy[pos.strategy_class].closed_trades.append(closed)
            realized_cumulative += closed.closed_pnl_dollars
        open_positions = still_open

        # 2. Fill ALL pending orders at this snap. Each is risk-
        #    evaluated against the AGGREGATE open positions (capital
        #    cap is shared across strategies).
        new_pending: list[tuple[str, MultiLegOrder, datetime]] = []
        for strat_id, pending, submitted_ts in pending_orders:
            proposed = _open_position_from_order(
                pending, snap, cost,
                position_id=f"pos_{next_position_id}",
            )
            if proposed is None:
                portfolio.aggregate_rejected_orders.append(RejectedOrder(
                    submitted_ts=submitted_ts,
                    fill_attempted_ts=snap.ts_utc,
                    strategy_tag=pending.tag,
                    reason="unfillable_at_next_snap",
                    proposed_capital_at_risk=0.0,
                ))
                per_strategy[strat_id].rejected_orders.append(
                    portfolio.aggregate_rejected_orders[-1]
                )
                continue
            if risk is not None:
                decision = risk.evaluate_order(
                    proposed_position=proposed,
                    open_positions=tuple(open_positions),
                    snapshot_history=tuple(snapshot_history),
                )
                if not decision.approved:
                    rec = RejectedOrder(
                        submitted_ts=submitted_ts,
                        fill_attempted_ts=snap.ts_utc,
                        strategy_tag=pending.tag,
                        reason=decision.reason,
                        proposed_capital_at_risk=(
                            decision.proposed_total_capital_at_risk
                        ),
                    )
                    portfolio.aggregate_rejected_orders.append(rec)
                    per_strategy[strat_id].rejected_orders.append(rec)
                    continue
            open_positions.append(proposed)
            next_position_id += 1
        pending_orders = new_pending

        # 3. Each strategy decides — multiple orders may queue
        #    simultaneously.
        ts = snap.ts_utc
        for strat in strategies:
            order = strat.on_chain(snap, tuple(open_positions))
            if order is not None:
                pending_orders.append((strat.id, order, ts))

        # 4. Aggregate snapshot P&L row.
        unrealized = sum(p.mark_dollars(snap) for p in open_positions)
        capital_at_risk = sum(p.total_capital_at_risk for p in open_positions)
        portfolio.aggregate_snapshot_pnl.append(SnapshotPnL(
            ts_utc=snap.ts_utc,
            underlying_price=snap.underlying_price,
            n_open_positions=len(open_positions),
            open_unrealized_dollars=unrealized,
            realized_dollars_cumulative=realized_cumulative,
            capital_at_risk_dollars=capital_at_risk,
        ))

    return portfolio
