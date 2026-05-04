"""Daily paper-trade orchestrator for the validated options portfolio.

Pipeline per invocation:

  1. Load every ingested chain snapshot for `(source_id, ticker)`
     via `iter_chain_snapshots`. The most-recent snapshot is
     "today" — every prior snapshot is replayed so the
     IvGatedStrategy wrappers re-build their `_atm_iv_history`
     state correctly. Replay during this phase is decision-only;
     no orders are routed for historical snapshots.

  2. For the "today" snapshot, the wrapped strategies' on_chain
     calls produce zero-or-more `MultiLegOrder` candidates. These
     are the entry decisions for the next session.

  3. The orchestrator emits a `LiveDecision` containing the
     entry candidates + the snapshot context. The CLI surface
     decides whether to print-only (`dry_run=True`) or route via
     `IbkrOptionsRouter.place_combo`.

V1 SCOPE — entries only.
  Closing of OPEN broker positions per `should_close` is OUT of
  scope for V1 because reconciling the runner's reconstructed
  open-position state against the broker's actual open positions
  is a non-trivial sync problem (the runner's positions come from
  historical replay, not from the broker). The operator handles
  closes manually per the 50% / 21 DTE / 200% loss discipline
  using TWS until the V2 reconciliation loop ships.

Per `14_options_volatility_selling.md` Phase E paper-trade. The
validated config (per Phase D-8 follow-up #6, 2026-05-03) is
SPY + PCS+IC+JL + IV<0.25 at $25K — see CLI defaults in
`tradegy live-options`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tradegy.options.chain import ChainSnapshot
from tradegy.options.chain_io import iter_chain_snapshots
from tradegy.options.portfolio_runner import (
    PortfolioBacktestResult,
    run_options_backtest_portfolio,
)
from tradegy.options.positions import MultiLegOrder, MultiLegPosition
from tradegy.options.risk import RiskConfig, RiskManager
from tradegy.options.strategies import IvGatedStrategy
from tradegy.options.strategy import ManagementRules, OptionStrategy


_log = logging.getLogger(__name__)


@dataclass
class LiveDecision:
    """One day's entry recommendations for the validated portfolio.

    Records the inputs (data source, strategies, gating params)
    AND the outputs (entry candidates, replay stats) so the
    decision is auditable post-hoc.
    """

    ts_utc: datetime
    underlying: str
    source_id: str
    snapshot_ts_utc: datetime
    snapshot_underlying_price: float
    n_replayed_snapshots: int
    strategy_ids: tuple[str, ...]
    iv_gate_max: float | None
    iv_gate_min: float | None
    declared_capital: float
    profit_take_pct: float
    loss_stop_pct: float
    dte_close: int
    entries: list[dict[str, Any]] = field(default_factory=list)
    replay_open_positions_at_today: int = 0
    replay_realized_pnl: float = 0.0

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["ts_utc"] = self.ts_utc.isoformat()
        out["snapshot_ts_utc"] = self.snapshot_ts_utc.isoformat()
        return out


def build_validated_portfolio(
    *,
    base_strategies: list[OptionStrategy],
    iv_gate_max: float | None,
    iv_gate_min: float | None,
    iv_gate_window_days: int = 252,
) -> list[OptionStrategy]:
    """Wrap each base strategy with `IvGatedStrategy` if either
    IV bound is set; otherwise return the bases unchanged.

    Mirrors the CLI's options-walk-forward gating semantics so
    the live decisions are identical to the validated backtest.
    """
    if iv_gate_max is None and iv_gate_min is None:
        return list(base_strategies)
    return [
        IvGatedStrategy(
            base=s,
            min_iv_rank=iv_gate_min,
            max_iv_rank=iv_gate_max,
            window_days=iv_gate_window_days,
        )
        for s in base_strategies
    ]


def generate_live_decision(
    *,
    base_strategies: list[OptionStrategy],
    source_id: str,
    ticker: str,
    declared_capital: float,
    iv_gate_max: float | None = None,
    iv_gate_min: float | None = None,
    iv_gate_window_days: int = 252,
    profit_take_pct: float = 0.50,
    loss_stop_pct: float = 2.0,
    dte_close: int = 21,
    chain_root: Path | None = None,
) -> LiveDecision:
    """Replay every ingested chain snapshot and return today's
    entry candidates.

    The strategy wrappers' internal IV-rank history builds up over
    the full replay. The portfolio runner's open-position state is
    built up identically to the backtest — entries / closes during
    replay are recorded but not surfaced. The OUTPUT
    (`LiveDecision.entries`) is the set of orders the strategies
    queue against the FINAL snapshot.

    Live caveat: the replay's "open positions at today" reflects
    BACKTEST positions, not broker positions. The operator must
    ensure broker state matches before routing entries.
    """
    strategies = build_validated_portfolio(
        base_strategies=base_strategies,
        iv_gate_max=iv_gate_max,
        iv_gate_min=iv_gate_min,
        iv_gate_window_days=iv_gate_window_days,
    )
    rules = ManagementRules(
        profit_take_pct=profit_take_pct,
        loss_stop_pct=loss_stop_pct,
        dte_close=dte_close,
    )
    risk = RiskManager(RiskConfig(declared_capital=declared_capital))

    snapshots = list(iter_chain_snapshots(
        source_id, ticker=ticker, root=chain_root,
    ))
    if not snapshots:
        raise RuntimeError(
            f"no snapshots ingested for source_id={source_id!r} "
            f"ticker={ticker!r}; run `tradegy ingest` first"
        )
    today = snapshots[-1]

    # Phase 1: replay everything-except-today to warm up state.
    # Use the standard portfolio runner over the warmup window —
    # it builds IV-rank history inside the wrappers AND tracks the
    # backtest's open-position state. We use the backtest's
    # open-position state at "end of warmup" as the input to the
    # final-snapshot decision.
    warmup_result = run_options_backtest_portfolio(
        strategies=strategies,
        snapshots=snapshots[:-1],  # exclude today
        rules=rules, risk=risk,
    )
    # End-of-warmup open positions come straight from the runner.
    # In live mode the operator MUST reconcile against IBKR before
    # routing — backtest-replay positions are not the same as
    # broker positions.
    open_positions = warmup_result.final_open_positions

    # Phase 2: today-only — call each strategy's on_chain with
    # the warmed state + open-positions tuple.
    entries: list[dict[str, Any]] = []
    for strat in strategies:
        # The wrapper expects to process every snapshot in order
        # to update its IV history; we already did that for the
        # warmup. For TODAY, the wrapper adds today's IV to its
        # history and (if the gate passes) delegates to the base.
        order = strat.on_chain(today, tuple(open_positions))
        if order is None:
            continue
        entries.append(_serialize_order(strat, order, today))

    return LiveDecision(
        ts_utc=datetime.now(tz=timezone.utc),
        underlying=ticker,
        source_id=source_id,
        snapshot_ts_utc=today.ts_utc,
        snapshot_underlying_price=today.underlying_price,
        n_replayed_snapshots=len(snapshots) - 1,
        strategy_ids=tuple(s.id for s in strategies),
        iv_gate_max=iv_gate_max,
        iv_gate_min=iv_gate_min,
        declared_capital=declared_capital,
        profit_take_pct=profit_take_pct,
        loss_stop_pct=loss_stop_pct,
        dte_close=dte_close,
        entries=entries,
        replay_open_positions_at_today=len(open_positions),
        replay_realized_pnl=warmup_result.realized_pnl_dollars,
    )


def write_decision(
    decision: LiveDecision, *, root: Path | None = None,
) -> Path:
    """Persist a decision as JSON under `data/live_options/decisions/`.

    Filename: `<snapshot_date>_<wallclock_iso>.json`. Snapshot
    date as the leading sortable key makes audit-trail browsing
    natural; wall-clock disambiguates re-runs of the same session.
    """
    from tradegy import config
    base = root or (config.repo_root() / "data" / "live_options" / "decisions")
    base.mkdir(parents=True, exist_ok=True)
    snap_date = decision.snapshot_ts_utc.date().isoformat()
    wallclock = decision.ts_utc.strftime("%Y%m%dT%H%M%S")
    out_path = base / f"{snap_date}_{wallclock}.json"
    out_path.write_text(json.dumps(decision.to_json(), indent=2, default=str))
    return out_path


# ── internals ─────────────────────────────────────────────────────


def _serialize_order(
    strategy: OptionStrategy,
    order: MultiLegOrder,
    snapshot: ChainSnapshot,
) -> dict[str, Any]:
    """Render a MultiLegOrder + chain context as a JSON-friendly
    dict.

    Includes per-leg quote info (bid/ask/iv) at decision time so
    the operator can sanity-check fillability before routing.
    """
    legs_out: list[dict[str, Any]] = []
    for leg_order in order.legs:
        chain_leg = None
        for cl in snapshot.for_expiry(leg_order.expiry):
            if cl.strike == leg_order.strike and cl.side == leg_order.side:
                chain_leg = cl
                break
        legs_out.append({
            "expiry": leg_order.expiry.isoformat(),
            "strike": leg_order.strike,
            "side": leg_order.side.value,
            "quantity": leg_order.quantity,
            "bid": chain_leg.bid if chain_leg else None,
            "ask": chain_leg.ask if chain_leg else None,
            "iv": chain_leg.iv if chain_leg else None,
        })
    return {
        "strategy_id": strategy.id,
        "tag": order.tag,
        "contracts": order.contracts,
        "underlying": snapshot.underlying,
        "underlying_price_at_decision": snapshot.underlying_price,
        "legs": legs_out,
    }
