"""OptionStrategy ABC + ManagementRules + close-decision logic.

A vol-selling strategy is a pure function from (current chain
snapshot, currently open positions) to "do I want to open a new
multi-leg position?". Management decisions (close at 50% profit /
21 DTE / 200% loss) are factored OUT of the strategy and into a
shared `ManagementRules` + `should_close` pair so all strategy
classes inherit identical management discipline (per
`14_options_volatility_selling.md` Phase B requirements: every
position has the same management triggers; only entry rules differ).

The runner uses both:

  - calls strategy.on_chain(snap, positions) to decide entries
  - calls should_close(position, snap, rules) on every open position
    to decide exits

Strategies do NOT decide their own exits. That guarantees the
management discipline can't be silently weakened by a buggy
strategy class.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from tradegy.options.chain import ChainSnapshot
from tradegy.options.positions import MultiLegOrder, MultiLegPosition


# ── Management rules ──────────────────────────────────────────────


@dataclass(frozen=True)
class ManagementRules:
    """Universal management discipline applied to every multi-leg
    position regardless of strategy class. Per doc 14 risk catalog
    items 4 (early-management discipline) + the tastytrade-published
    convention.

    `profit_take_pct`: close when unrealized P&L ≥ this fraction of
        the entry credit. 0.50 = "close at 50% of max profit." Pure
        debit positions skip this trigger (their max-profit isn't
        bounded the same way; pnl_pct_of_max_credit returns NaN).
    `dte_close`: close when nearest-leg DTE ≤ this. 21 = standard.
        Pin risk near expiration kills accounts; this trigger is
        non-negotiable.
    `loss_stop_pct`: close when unrealized loss ≥ this multiple of
        the entry credit. 2.0 = "close at 200% loss" (lose twice
        the credit received). Caps the per-position drawdown.
    """

    profit_take_pct: float = 0.50
    dte_close: int = 21
    loss_stop_pct: float = 2.0


def should_close(
    position: MultiLegPosition,
    snapshot: ChainSnapshot,
    rules: ManagementRules,
) -> str | None:
    """Return a close-reason string if any management trigger fires,
    else None.

    Triggers checked in priority order: DTE first (non-negotiable
    pin-risk floor), then profit-take, then loss-stop. When two
    triggers fire on the same snapshot the highest-priority one
    is reported — important for the audit trail (we want to know
    why we closed, and "21 DTE" is more meaningful than "happened
    to be at 50% profit on day 24" if both were true).
    """
    if not position.open:
        return None

    # 1. DTE — load-bearing per doc 14, runs first.
    dte = position.days_to_expiry(snapshot.ts_utc)
    if dte <= rules.dte_close:
        return f"dte_close: nearest leg at {dte} DTE (rule ≤ {rules.dte_close})"

    # 2. Profit take.
    pnl_pct = position.pnl_pct_of_max_credit(snapshot)
    if pnl_pct == pnl_pct and pnl_pct >= rules.profit_take_pct:  # NaN-safe
        return (
            f"profit_take: pnl {pnl_pct * 100:.1f}% of credit "
            f"(rule ≥ {rules.profit_take_pct * 100:.0f}%)"
        )

    # 3. Loss stop. pnl_pct_of_max_credit goes negative when we're
    # losing on a credit position; -2.0 means the unrealized loss
    # equals 2× the credit received.
    if pnl_pct == pnl_pct and pnl_pct <= -rules.loss_stop_pct:
        return (
            f"loss_stop: pnl {pnl_pct * 100:.1f}% of credit "
            f"(rule ≤ -{rules.loss_stop_pct * 100:.0f}%)"
        )

    return None


# ── Strategy ABC ──────────────────────────────────────────────────


class OptionStrategy(ABC):
    """Stateless decision function for a vol-selling strategy.

    Implementations override `on_chain` to inspect the current
    snapshot + currently open multi-leg positions and decide whether
    to open a new position. They MUST NOT decide closes — that's the
    runner + ManagementRules' job.

    `id` is a string used in the audit trail / position tagging.
    """

    id: str = "<override>"

    @abstractmethod
    def on_chain(
        self,
        snapshot: ChainSnapshot,
        open_positions: tuple[MultiLegPosition, ...],
    ) -> MultiLegOrder | None:
        """Return a MultiLegOrder to open at the next snapshot's
        chain, or None to skip this snapshot.

        The runner queues the returned order and fills it at the
        NEXT snapshot — not the current one. This avoids any
        same-bar lookahead.
        """
        ...
