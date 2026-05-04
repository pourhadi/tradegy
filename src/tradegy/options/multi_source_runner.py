"""Multi-underlying portfolio runner — shared capital across
several option-chain sources.

The single-source `run_options_backtest_portfolio` runs N strategies
against ONE chain stream. For retail capital ($5K target) the
binding constraint is `concurrent positions`, not per-trade edge —
diversifying across underlyings (SPY + IWM + QQQ + DIA) lets the
RiskManager fit MORE positions in the same dollar cap because
their max-loss profiles aren't perfectly correlated.

Design:

  - Each source gets its OWN list of strategies. The strategy list
    is INDEPENDENT per source (PCS bound to SPY isn't the same
    instance as PCS bound to IWM — different IV-rank histories,
    different concentration filters).

  - Snapshots from all sources are merged by ts_utc. Per-tick:
      - Each source's strategies see ONLY that source's snapshot.
      - All produced orders compete for the shared RiskManager
        capital cap.
      - All open positions are managed together (50%/21DTE/200%
        applies regardless of underlying).

  - Snapshot merging assumes all sources publish at the same
    daily ts_utc (typical for US equity options — all close at
    16:00 ET, ORATS publishes EOD at ~16:46 ET). When timestamps
    differ across sources, the merge groups by date and uses each
    source's snap with the largest ts within that date.

Per `14_options_volatility_selling.md` 2026-05-04 multi-underlying
expansion: built to test whether SPY+IWM+QQQ+DIA at shared $5K
captures more trades than SPY-solo at $5K.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from tradegy.options.chain import ChainSnapshot
from tradegy.options.chain_io import iter_chain_snapshots
from tradegy.options.cost_model import OptionCostModel
from tradegy.options.portfolio_runner import (
    PortfolioBacktestResult,
    run_options_backtest_portfolio,
)
from tradegy.options.positions import MultiLegOrder, MultiLegPosition
from tradegy.options.risk import RiskManager
from tradegy.options.runner import (
    ClosedTrade,
    RejectedOrder,
    SnapshotPnL,
    OptionsBacktestResult,
    _close_position,
    _open_position_from_order,
)
from tradegy.options.strategy import (
    ManagementRules,
    OptionStrategy,
    should_close,
)


_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceSpec:
    """One underlying's source + ticker + strategy list.

    Each strategy in `strategies` is bound implicitly to this
    source — the runner only feeds it `source`'s snapshots.
    Strategy ids must be globally unique across all SourceSpecs
    (otherwise the per-strategy result dict collides).
    """

    source_id: str
    ticker: str
    strategies: tuple[OptionStrategy, ...]


@dataclass
class MultiSourcePortfolioResult:
    """Aggregate result across all sources. Per-source breakdown
    is preserved for inspection.
    """

    n_ticks_seen: int = 0
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    rejected_orders: list[RejectedOrder] = field(default_factory=list)
    snapshot_pnl: list[SnapshotPnL] = field(default_factory=list)
    per_source: dict[str, OptionsBacktestResult] = field(default_factory=dict)
    final_open_positions: tuple[MultiLegPosition, ...] = field(default_factory=tuple)

    @property
    def n_closed_trades(self) -> int:
        return len(self.closed_trades)

    @property
    def realized_pnl_dollars(self) -> float:
        return sum(t.closed_pnl_dollars for t in self.closed_trades)

    @property
    def hit_rate(self) -> float:
        if not self.closed_trades:
            return float("nan")
        wins = sum(1 for t in self.closed_trades if t.closed_pnl_dollars > 0)
        return wins / len(self.closed_trades)


def run_options_backtest_multi_source(
    *,
    sources: list[SourceSpec],
    coverage_start: datetime | None = None,
    coverage_end: datetime | None = None,
    cost: OptionCostModel | None = None,
    rules: ManagementRules | None = None,
    rules_by_id: dict[str, ManagementRules] | None = None,
    risk: RiskManager | None = None,
    chain_root: Path | None = None,
) -> MultiSourcePortfolioResult:
    """Run a multi-underlying portfolio with shared capital cap.

    Loads each source's snapshots, groups by trade-date, and per
    trade-date runs all bound strategies against their respective
    underlying's snapshot. The RiskManager is shared — capital
    usage from any source's positions counts against the total cap.

    Per-strategy ids must be globally unique. The simplest pattern:
    construct each strategy with an id that includes the underlying
    (e.g., `iv_gated_max0.25_pcs_spy`, `..._iwm`, etc.).
    """
    cost = cost or OptionCostModel()
    default_rules = rules or ManagementRules()
    rules_by_id = rules_by_id or {}

    # Load + group snapshots per source.
    snaps_by_source: dict[str, list[ChainSnapshot]] = {}
    for spec in sources:
        snaps = list(iter_chain_snapshots(
            spec.source_id, ticker=spec.ticker,
            start=coverage_start, end=coverage_end,
            root=chain_root,
        ))
        snaps_by_source[spec.source_id] = snaps

    # Build a per-trade-date index so we can iterate the merged
    # timeline. For each date, we keep the LATEST snapshot per
    # source (handles the rare case of multiple intraday snapshots).
    snaps_by_date: dict[date, dict[str, ChainSnapshot]] = {}
    for source_id, snaps in snaps_by_source.items():
        for snap in snaps:
            d = snap.ts_utc.date()
            if d not in snaps_by_date:
                snaps_by_date[d] = {}
            existing = snaps_by_date[d].get(source_id)
            if existing is None or snap.ts_utc > existing.ts_utc:
                snaps_by_date[d][source_id] = snap

    sorted_dates = sorted(snaps_by_date.keys())
    if not sorted_dates:
        raise RuntimeError(
            f"no snapshots found across {[s.source_id for s in sources]}"
        )

    # Per-source-strategy result accumulators. Strategy id is the
    # primary key; if collisions exist across sources we error
    # explicitly (avoids silently merging two strategies' trades).
    per_strategy: dict[str, OptionsBacktestResult] = {}
    for spec in sources:
        for strat in spec.strategies:
            if strat.id in per_strategy:
                raise ValueError(
                    f"strategy id collision across sources: "
                    f"{strat.id!r} appears in multiple SourceSpecs. "
                    "Construct strategies with source-suffixed ids "
                    "(e.g., `..._spy`, `..._iwm`)."
                )
            per_strategy[strat.id] = OptionsBacktestResult(
                strategy_id=strat.id, n_snapshots_seen=0,
            )
    # Also key per-source for the final breakdown (sum across
    # strategies bound to that source).
    per_source: dict[str, OptionsBacktestResult] = {
        spec.source_id: OptionsBacktestResult(
            strategy_id=f"<source:{spec.source_id}>", n_snapshots_seen=0,
        )
        for spec in sources
    }

    result = MultiSourcePortfolioResult(per_source=per_source)

    # Per-source pending orders so each source's queue is
    # independent (PCS-on-SPY can be pending while IC-on-IWM
    # fills).
    pending_orders: list[tuple[str, str, MultiLegOrder, datetime]] = []
    # tuple = (source_id, strategy_id, order, submitted_ts)
    open_positions: list[MultiLegPosition] = []
    next_position_id = 0
    realized_cumulative = 0.0
    snap_history: list[ChainSnapshot] = []

    for d in sorted_dates:
        date_snaps = snaps_by_date[d]
        # Use any source's snap as the "tick anchor" (for ts_utc).
        # All sources should have the same ts on a normal trading
        # day; pick the first available.
        anchor_snap = next(iter(date_snaps.values()))
        result.n_ticks_seen += 1
        for r in per_strategy.values():
            r.n_snapshots_seen += 1
        snap_history.append(anchor_snap)

        # 1. Mark + management on every open position. Each is
        # marked against the snapshot of ITS underlying.
        still_open: list[MultiLegPosition] = []
        for pos in open_positions:
            pos_underlying = pos.underlyings[0] if pos.underlyings else None
            # Find the source whose ticker matches the position's
            # underlying. Falls back to anchor_snap if not present
            # (shouldn't happen — every position was opened against
            # one of the sources).
            mark_snap = anchor_snap
            for source_id, snap in date_snaps.items():
                if snap.underlying == pos_underlying:
                    mark_snap = snap
                    break
            pos_rules = rules_by_id.get(pos.strategy_class, default_rules)
            reason = should_close(pos, mark_snap, pos_rules)
            if reason is None:
                still_open.append(pos)
                continue
            closed = _close_position(pos, mark_snap, cost, reason)
            result.closed_trades.append(closed)
            per_strategy[pos.strategy_class].closed_trades.append(closed)
            # Per-source breakdown: find the source whose ticker
            # matches the closed position's underlying.
            for source_id, snap in date_snaps.items():
                if snap.underlying == pos_underlying:
                    per_source[source_id].closed_trades.append(closed)
                    break
            realized_cumulative += closed.closed_pnl_dollars
        open_positions = still_open

        # 2. Fill ALL pending orders queued from prior tick.
        new_pending: list[tuple[str, str, MultiLegOrder, datetime]] = []
        for source_id, strat_id, pending, submitted_ts in pending_orders:
            fill_snap = date_snaps.get(source_id)
            if fill_snap is None:
                # Source had no snap on this date (e.g., holiday
                # mismatch). Reject + log.
                rec = RejectedOrder(
                    submitted_ts=submitted_ts,
                    fill_attempted_ts=anchor_snap.ts_utc,
                    strategy_tag=pending.tag,
                    reason=f"no snapshot for source {source_id}",
                    proposed_capital_at_risk=0.0,
                )
                result.rejected_orders.append(rec)
                continue
            proposed = _open_position_from_order(
                pending, fill_snap, cost,
                position_id=f"pos_{next_position_id}",
            )
            if proposed is None:
                rec = RejectedOrder(
                    submitted_ts=submitted_ts,
                    fill_attempted_ts=fill_snap.ts_utc,
                    strategy_tag=pending.tag,
                    reason="unfillable_at_next_snap",
                    proposed_capital_at_risk=0.0,
                )
                result.rejected_orders.append(rec)
                per_strategy[strat_id].rejected_orders.append(rec)
                continue
            if risk is not None:
                decision = risk.evaluate_order(
                    proposed_position=proposed,
                    open_positions=tuple(open_positions),
                    snapshot_history=tuple(snap_history),
                )
                if not decision.approved:
                    rec = RejectedOrder(
                        submitted_ts=submitted_ts,
                        fill_attempted_ts=fill_snap.ts_utc,
                        strategy_tag=pending.tag,
                        reason=decision.reason,
                        proposed_capital_at_risk=(
                            decision.proposed_total_capital_at_risk
                        ),
                    )
                    result.rejected_orders.append(rec)
                    per_strategy[strat_id].rejected_orders.append(rec)
                    continue
            open_positions.append(proposed)
            next_position_id += 1
        pending_orders = new_pending

        # 3. Each source's strategies decide against THEIR snap.
        ts = anchor_snap.ts_utc
        for spec in sources:
            spec_snap = date_snaps.get(spec.source_id)
            if spec_snap is None:
                continue
            for strat in spec.strategies:
                order = strat.on_chain(spec_snap, tuple(open_positions))
                if order is not None:
                    pending_orders.append(
                        (spec.source_id, strat.id, order, ts)
                    )

        # 4. Aggregate snapshot P&L row (single row per tick;
        # unrealized summed across all underlyings).
        unrealized = 0.0
        capital_at_risk = 0.0
        for pos in open_positions:
            pos_underlying = pos.underlyings[0] if pos.underlyings else None
            mark_snap = anchor_snap
            for source_id, snap in date_snaps.items():
                if snap.underlying == pos_underlying:
                    mark_snap = snap
                    break
            unrealized += pos.mark_dollars(mark_snap)
            capital_at_risk += pos.total_capital_at_risk
        result.snapshot_pnl.append(SnapshotPnL(
            ts_utc=anchor_snap.ts_utc,
            underlying_price=anchor_snap.underlying_price,
            n_open_positions=len(open_positions),
            open_unrealized_dollars=unrealized,
            realized_dollars_cumulative=realized_cumulative,
            capital_at_risk_dollars=capital_at_risk,
        ))

    result.final_open_positions = tuple(open_positions)
    return result


@dataclass
class MultiSourceWalkForwardWindow:
    """One (train, test) split for the multi-source runner."""

    index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    in_sample: MultiSourcePortfolioResult | None = None
    out_of_sample: MultiSourcePortfolioResult | None = None


@dataclass
class MultiSourceWalkForwardSummary:
    sources: tuple[str, ...]
    train_years: float
    test_years: float
    roll_years: float
    coverage_start: datetime
    coverage_end: datetime
    windows: list[MultiSourceWalkForwardWindow] = field(default_factory=list)
    avg_in_sample_sharpe: float = 0.0
    avg_oos_sharpe: float = 0.0
    worst_window_oos_sharpe: float = 0.0
    avg_in_sample_trades: float = 0.0
    avg_oos_trades: float = 0.0
    passed: bool = False
    fail_reason: str = ""


def run_multi_source_walk_forward(
    *,
    sources: list[SourceSpec],
    coverage_start: datetime,
    coverage_end: datetime,
    train_years: float = 3.0,
    test_years: float = 1.0,
    roll_years: float = 1.0,
    cost: OptionCostModel | None = None,
    rules: ManagementRules | None = None,
    risk: RiskManager | None = None,
    chain_root: Path | None = None,
) -> MultiSourceWalkForwardSummary:
    """Rolling walk-forward over a multi-source portfolio.

    Each window: run the multi-source backtest once for train range,
    once for test range, with the SAME strategy instances (so the
    IV-rank history accumulates correctly across both halves).

    Identical gate to the single-source walk-forward: avg OOS Sharpe
    ≥ 50% × avg IS Sharpe AND avg IS Sharpe > 0.
    """
    import statistics
    from datetime import timedelta as _td

    from tradegy.options.walk_forward import trade_dollar_sharpe

    train_delta = _td(days=int(round(train_years * 365.25)))
    test_delta = _td(days=int(round(test_years * 365.25)))
    roll_delta = _td(days=int(round(roll_years * 365.25)))

    windows: list[MultiSourceWalkForwardWindow] = []
    idx = 0
    train_start = coverage_start
    while True:
        train_end = train_start + train_delta
        test_start = train_end
        test_end = test_start + test_delta
        if test_end > coverage_end:
            break
        windows.append(MultiSourceWalkForwardWindow(
            index=idx,
            train_start=train_start, train_end=train_end,
            test_start=test_start, test_end=test_end,
        ))
        idx += 1
        train_start = train_start + roll_delta
    if not windows:
        raise ValueError(
            "multi-source walk-forward: coverage span too short for "
            "the requested train+test windows"
        )

    is_sharpes: list[float] = []
    oos_sharpes: list[float] = []
    is_trades: list[int] = []
    oos_trades: list[int] = []

    for win in windows:
        in_res = run_options_backtest_multi_source(
            sources=sources,
            coverage_start=win.train_start, coverage_end=win.train_end,
            cost=cost, rules=rules, risk=risk, chain_root=chain_root,
        )
        oos_res = run_options_backtest_multi_source(
            sources=sources,
            coverage_start=win.test_start, coverage_end=win.test_end,
            cost=cost, rules=rules, risk=risk, chain_root=chain_root,
        )
        win.in_sample = in_res
        win.out_of_sample = oos_res
        is_sharpes.append(trade_dollar_sharpe(in_res.closed_trades))
        oos_sharpes.append(trade_dollar_sharpe(oos_res.closed_trades))
        is_trades.append(in_res.n_closed_trades)
        oos_trades.append(oos_res.n_closed_trades)

    summary = MultiSourceWalkForwardSummary(
        sources=tuple(s.source_id for s in sources),
        train_years=train_years,
        test_years=test_years,
        roll_years=roll_years,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        windows=windows,
        avg_in_sample_sharpe=statistics.fmean(is_sharpes) if is_sharpes else 0.0,
        avg_oos_sharpe=statistics.fmean(oos_sharpes) if oos_sharpes else 0.0,
        worst_window_oos_sharpe=min(oos_sharpes) if oos_sharpes else 0.0,
        avg_in_sample_trades=statistics.fmean(is_trades) if is_trades else 0.0,
        avg_oos_trades=statistics.fmean(oos_trades) if oos_trades else 0.0,
    )
    if summary.avg_in_sample_sharpe <= 0:
        summary.passed = False
        summary.fail_reason = (
            f"avg in-sample Sharpe ({summary.avg_in_sample_sharpe:.3f}) "
            "is not positive — no in-sample edge"
        )
    elif summary.avg_oos_sharpe < 0.5 * summary.avg_in_sample_sharpe:
        ratio = summary.avg_oos_sharpe / summary.avg_in_sample_sharpe
        summary.passed = False
        summary.fail_reason = (
            f"avg OOS Sharpe ({summary.avg_oos_sharpe:.3f}) is "
            f"< 50% of in-sample ({summary.avg_in_sample_sharpe:.3f}); "
            f"ratio = {ratio:.2f}"
        )
    else:
        summary.passed = True
    return summary


def build_iv_gated_strategies_per_source(
    *,
    underlyings: list[str],
    base_strategy_factories: list[callable],  # each callable returns OptionStrategy
    iv_gate_max: float | None,
    iv_gate_min: float | None = None,
    iv_gate_window_days: int = 252,
) -> list[SourceSpec]:
    """Build one SourceSpec per underlying with the IV-gated wrapped
    strategies — and per-underlying-suffixed ids so the multi-source
    runner can disambiguate them.

    `underlyings` is e.g. ["SPY", "IWM", "QQQ", "DIA"]; each maps
    to source_id `<lower(ticker)>_options_chain`.
    """
    from tradegy.options.strategies import IvGatedStrategy

    out: list[SourceSpec] = []
    for ticker in underlyings:
        source_id = f"{ticker.lower()}_options_chain"
        strategies = []
        for factory in base_strategy_factories:
            base = factory()
            wrapped = IvGatedStrategy(
                base=base,
                min_iv_rank=iv_gate_min,
                max_iv_rank=iv_gate_max,
                window_days=iv_gate_window_days,
            )
            # Suffix the wrapper's id with the underlying so it's
            # globally unique across sources.
            from dataclasses import replace
            wrapped = replace(wrapped, id=f"{wrapped.id}_{ticker.lower()}")
            strategies.append(wrapped)
        out.append(SourceSpec(
            source_id=source_id, ticker=ticker,
            strategies=tuple(strategies),
        ))
    return out
