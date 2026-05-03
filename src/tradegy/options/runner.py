"""Multi-leg options backtest runner.

Iterates a sequence of ChainSnapshots in chronological order and:

  1. Marks every open MultiLegPosition to the current chain.
  2. Calls `should_close(...)` on each open position; closes any
     that trigger management rules. Records the close fill +
     realized P&L as a ClosedTrade.
  3. Fills any pending MultiLegOrder queued from the prior
     snapshot's strategy decision. The fill happens at the
     CURRENT snap's chain (no same-bar lookahead — the strategy
     decided to enter on snap[i-1] and fills at snap[i]).
  4. Calls strategy.on_chain(snap, open_positions). If a
     MultiLegOrder is returned, queue it for next-snap fill.

Returns an OptionsBacktestResult with realized + unrealized P&L
trajectories, closed-trade records, and aggregate stats.

Per `14_options_volatility_selling.md` Phase B-2: the runner is
the single point of management discipline — strategies don't
decide closes. This means a buggy strategy class can't accidentally
hold a losing position past the 200% loss cap or skip the 21 DTE
exit.

Also per doc 14 risk-catalog item 1 (defined-risk only): the
runner refuses to enter a position whose
`max_loss_per_contract <= 0` (would be undefined-risk if max-loss
math doesn't bound the trade).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

from tradegy.options.chain import ChainSnapshot, OptionLeg, OptionSide
from tradegy.options.cost_model import OptionCostModel
from tradegy.options.positions import (
    LegOrder,
    MultiLegOrder,
    MultiLegPosition,
    OptionPosition,
    compute_max_loss_per_contract,
)
from tradegy.options.strategy import (
    ManagementRules,
    OptionStrategy,
    should_close,
)


# ── Result dataclasses ────────────────────────────────────────────


@dataclass(frozen=True)
class ClosedTrade:
    """One MultiLegPosition's complete lifecycle: entry + close.

    `closed_pnl_dollars` is realized P&L net of commissions (open +
    close round-trip). Positive = profit, negative = loss.
    """

    position_id: str
    strategy_class: str
    contracts: int
    entry_ts: datetime
    closed_ts: datetime
    entry_credit_per_share: float
    closed_credit_per_share: float
    closed_pnl_per_share: float
    closed_pnl_dollars: float
    open_commission: float
    close_commission: float
    closed_reason: str
    expiries: tuple[date, ...]


@dataclass
class SnapshotPnL:
    """One row in the per-snapshot P&L trajectory."""

    ts_utc: datetime
    underlying_price: float
    n_open_positions: int
    open_unrealized_dollars: float
    realized_dollars_cumulative: float
    capital_at_risk_dollars: float


@dataclass
class OptionsBacktestResult:
    strategy_id: str
    n_snapshots_seen: int
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    snapshot_pnl: list[SnapshotPnL] = field(default_factory=list)

    @property
    def n_closed_trades(self) -> int:
        return len(self.closed_trades)

    @property
    def realized_pnl_dollars(self) -> float:
        return sum(t.closed_pnl_dollars for t in self.closed_trades)

    @property
    def n_winners(self) -> int:
        return sum(1 for t in self.closed_trades if t.closed_pnl_dollars > 0)

    @property
    def n_losers(self) -> int:
        return sum(1 for t in self.closed_trades if t.closed_pnl_dollars < 0)

    @property
    def hit_rate(self) -> float:
        if self.n_closed_trades == 0:
            return float("nan")
        return self.n_winners / self.n_closed_trades

    @property
    def avg_pnl_per_trade(self) -> float:
        if self.n_closed_trades == 0:
            return float("nan")
        return self.realized_pnl_dollars / self.n_closed_trades

    @property
    def max_drawdown_dollars(self) -> float:
        """Worst peak-to-trough on the realized P&L trajectory.
        Unrealized swings are excluded so the metric reflects
        actual realized losses.
        """
        if not self.snapshot_pnl:
            return 0.0
        peak = self.snapshot_pnl[0].realized_dollars_cumulative
        worst = 0.0
        for snap in self.snapshot_pnl:
            cur = snap.realized_dollars_cumulative
            peak = max(peak, cur)
            dd = cur - peak
            if dd < worst:
                worst = dd
        return worst


# ── Runner ────────────────────────────────────────────────────────


def run_options_backtest(
    *,
    strategy: OptionStrategy,
    snapshots: Iterable[ChainSnapshot],
    cost: OptionCostModel | None = None,
    rules: ManagementRules | None = None,
) -> OptionsBacktestResult:
    """Run `strategy` against `snapshots` in chronological order.

    `snapshots` should already be sorted by ts_utc (the iterator
    produced by `iter_chain_snapshots` is). The runner does not
    re-sort.

    `cost` defaults to OptionCostModel() (IBKR-retail-ish).
    `rules` defaults to the canonical ManagementRules (50% / 21 DTE
    / 200% loss).

    Returns OptionsBacktestResult with full per-snapshot P&L and
    per-trade close records.
    """
    cost = cost or OptionCostModel()
    rules = rules or ManagementRules()

    open_positions: list[MultiLegPosition] = []
    pending_order: MultiLegOrder | None = None
    realized_cumulative = 0.0
    next_position_id = 0

    result = OptionsBacktestResult(
        strategy_id=strategy.id, n_snapshots_seen=0,
    )

    for snap in snapshots:
        result.n_snapshots_seen += 1

        # 1. Mark + management on existing positions.
        still_open: list[MultiLegPosition] = []
        for pos in open_positions:
            reason = should_close(pos, snap, rules)
            if reason is None:
                still_open.append(pos)
                continue
            closed = _close_position(pos, snap, cost, reason)
            result.closed_trades.append(closed)
            realized_cumulative += closed.closed_pnl_dollars
        open_positions = still_open

        # 2. Fill any order queued from the prior snap.
        if pending_order is not None:
            new_pos = _open_position_from_order(
                pending_order, snap, cost, position_id=f"pos_{next_position_id}",
            )
            pending_order = None
            if new_pos is not None:
                open_positions.append(new_pos)
                next_position_id += 1

        # 3. Strategy decision for the next snap.
        order = strategy.on_chain(snap, tuple(open_positions))
        if order is not None:
            pending_order = order

        # 4. Snapshot P&L row.
        unrealized = sum(p.mark_dollars(snap) for p in open_positions)
        capital_at_risk = sum(p.total_capital_at_risk for p in open_positions)
        result.snapshot_pnl.append(SnapshotPnL(
            ts_utc=snap.ts_utc,
            underlying_price=snap.underlying_price,
            n_open_positions=len(open_positions),
            open_unrealized_dollars=unrealized,
            realized_dollars_cumulative=realized_cumulative,
            capital_at_risk_dollars=capital_at_risk,
        ))

    return result


# ── Internals ─────────────────────────────────────────────────────


def _open_position_from_order(
    order: MultiLegOrder,
    snap: ChainSnapshot,
    cost: OptionCostModel,
    *,
    position_id: str,
) -> MultiLegPosition | None:
    """Look each leg of the order up in `snap`'s chain, fill at the
    cost-model's mid ± offset, and construct a MultiLegPosition.

    Returns None if any leg can't be filled (missing strike, dead
    quote) or if the resulting position would have undefined risk
    (max_loss_per_contract <= 0). No-fallback per the docs: the
    runner doesn't substitute a "close enough" leg silently.
    """
    legs: list[OptionPosition] = []
    fill_prices: list[float] = []
    for leg_order in order.legs:
        chain_leg = _find_leg(
            snap, leg_order.expiry, leg_order.strike, leg_order.side,
        )
        if chain_leg is None:
            return None
        fill_px = cost.fill_price(chain_leg, signed_quantity=leg_order.quantity)
        if fill_px <= 0.0:
            return None
        legs.append(OptionPosition(
            contract_id=OptionPosition.make_contract_id(
                snap.underlying, leg_order.expiry,
                leg_order.strike, leg_order.side,
            ),
            underlying=snap.underlying,
            expiry=leg_order.expiry,
            strike=leg_order.strike,
            side=leg_order.side,
            multiplier=chain_leg.multiplier,
            quantity=leg_order.quantity,
            entry_price=fill_px,
            entry_ts=snap.ts_utc,
        ))
        fill_prices.append(fill_px)

    # Net per-share credit (positive) or debit (negative).
    cost_to_open_per_share = sum(l.cost_to_open() for l in legs)
    entry_credit = -cost_to_open_per_share

    max_loss = compute_max_loss_per_contract(legs, entry_credit)
    if max_loss <= 0.0:
        # Undefined-risk shape — refuse per doc 14 risk-catalog #1.
        return None

    return MultiLegPosition(
        position_id=position_id,
        strategy_class=order.tag,
        contracts=order.contracts,
        legs=tuple(legs),
        entry_ts=snap.ts_utc,
        entry_credit_per_share=entry_credit,
        max_loss_per_contract=max_loss,
    )


def _close_position(
    pos: MultiLegPosition,
    snap: ChainSnapshot,
    cost: OptionCostModel,
    reason: str,
) -> ClosedTrade:
    """Compute close fills + realized P&L for `pos` at `snap`.

    Each leg is closed at the cost-model's mid ± offset using the
    OPPOSITE signed quantity (closing reverses direction). The
    closed credit per share is `-sum(close cash flow per share)` —
    same convention as entry credit. Realized per-share P&L is
    entry_credit - close_credit (we received entry_credit, paid
    close_credit; net = profit if entry > close).
    """
    close_per_share = 0.0
    n_legs = len(pos.legs)
    multiplier = pos.legs[0].multiplier if pos.legs else 100

    for leg in pos.legs:
        chain_leg = _find_leg(snap, leg.expiry, leg.strike, leg.side)
        if chain_leg is None:
            # Sparse chain at close: mark to intrinsic. Same fallback
            # as MultiLegPosition.mark_to_market.
            if leg.side == OptionSide.CALL:
                close_px = max(snap.underlying_price - leg.strike, 0.0)
            else:
                close_px = max(leg.strike - snap.underlying_price, 0.0)
        else:
            # Closing reverses sign.
            close_px = cost.fill_price(chain_leg, signed_quantity=-leg.quantity)
        # Closing cash flow per share for this leg = -quantity * close_px.
        close_per_share += -leg.quantity * close_px

    closed_credit = -close_per_share  # convention: + means we received on close
    pnl_per_share = pos.entry_credit_per_share - closed_credit
    pnl_dollars_gross = pnl_per_share * multiplier * pos.contracts

    open_commission = cost.commission_for_legs(n_legs, contracts=pos.contracts)
    close_commission = cost.commission_for_legs(n_legs, contracts=pos.contracts)
    pnl_dollars_net = pnl_dollars_gross - open_commission - close_commission

    return ClosedTrade(
        position_id=pos.position_id,
        strategy_class=pos.strategy_class,
        contracts=pos.contracts,
        entry_ts=pos.entry_ts,
        closed_ts=snap.ts_utc,
        entry_credit_per_share=pos.entry_credit_per_share,
        closed_credit_per_share=closed_credit,
        closed_pnl_per_share=pnl_per_share,
        closed_pnl_dollars=pnl_dollars_net,
        open_commission=open_commission,
        close_commission=close_commission,
        closed_reason=reason,
        expiries=pos.expiries,
    )


def _find_leg(
    snap: ChainSnapshot, expiry: date, strike: float, side: OptionSide,
) -> OptionLeg | None:
    for leg in snap.for_expiry(expiry):
        if leg.strike == strike and leg.side == side:
            return leg
    return None
