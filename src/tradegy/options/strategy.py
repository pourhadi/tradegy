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

    Credit-position triggers (iron condor, credit spread, strangle):
      `profit_take_pct`: close when unrealized P&L ≥ this fraction
          of entry credit. 0.50 = "close at 50% of max profit."
      `loss_stop_pct`: close when unrealized loss ≥ this multiple
          of entry credit. 2.0 = "close at 200% loss" (lose twice
          the credit received).

    Debit-position triggers (calendar spread, long verticals):
      `profit_take_pct_of_debit`: close when unrealized P&L ≥ this
          fraction of the debit paid. 0.25 = "close at 25% gain on
          debit." Common practitioner convention for calendars.
          Defaults to None (no profit-take for debit positions —
          DTE rule alone manages them).
      `loss_stop_pct_of_debit`: close when unrealized loss ≥ this
          fraction of the debit paid. 0.50 = "close at 50% loss
          on debit." Defaults to None.

    Universal:
      `dte_close`: close when nearest-leg DTE ≤ this. 21 = standard.
          Pin risk near expiration is the load-bearing risk for ALL
          multi-leg positions; this trigger applies regardless of
          credit or debit shape.

    The runner dispatches the right trigger based on each position's
    entry_credit sign — see `should_close` below. Debit triggers
    are off by default to preserve backward-compat; calendar
    strategies pass an explicit ManagementRules instance with
    them set.
    """

    profit_take_pct: float = 0.50
    dte_close: int = 21
    loss_stop_pct: float = 2.0
    profit_take_pct_of_debit: float | None = None
    loss_stop_pct_of_debit: float | None = None


def should_close(
    position: MultiLegPosition,
    snapshot: ChainSnapshot,
    rules: ManagementRules,
) -> str | None:
    """Return a close-reason string if any management trigger fires,
    else None.

    Trigger priority (load-bearing — operator gets the most-
    critical reason in the audit trail when multiple fire):

      1. DTE: pin-risk floor, applies to every position regardless
         of credit/debit shape.
      2. Credit-position profit-take + loss-stop (when entry was a
         credit).
      3. Debit-position profit-take + loss-stop (when entry was a
         debit AND the rules.*_of_debit fields are set).

    Credit and debit branches are mutually exclusive — a position
    has one or the other shape, never both. The pnl_pct_of_*
    methods return NaN for the wrong shape, so the NaN-safe
    comparison naturally short-circuits the inapplicable branch.
    """
    if not position.open:
        return None

    # 1. DTE — universal trigger.
    dte = position.days_to_expiry(snapshot.ts_utc)
    if dte <= rules.dte_close:
        return f"dte_close: nearest leg at {dte} DTE (rule ≤ {rules.dte_close})"

    # 2. Credit-position branch.
    pnl_pct_credit = position.pnl_pct_of_max_credit(snapshot)
    if pnl_pct_credit == pnl_pct_credit:  # NaN-safe → only fires for credit positions
        if pnl_pct_credit >= rules.profit_take_pct:
            return (
                f"profit_take: pnl {pnl_pct_credit * 100:.1f}% of credit "
                f"(rule ≥ {rules.profit_take_pct * 100:.0f}%)"
            )
        if pnl_pct_credit <= -rules.loss_stop_pct:
            return (
                f"loss_stop: pnl {pnl_pct_credit * 100:.1f}% of credit "
                f"(rule ≤ -{rules.loss_stop_pct * 100:.0f}%)"
            )
        return None

    # 3. Debit-position branch (only when the *_of_debit rules are set).
    pnl_pct_debit = position.pnl_pct_of_debit(snapshot)
    if pnl_pct_debit != pnl_pct_debit:  # NaN → no shape applies (zero credit zero debit)
        return None
    if (
        rules.profit_take_pct_of_debit is not None
        and pnl_pct_debit >= rules.profit_take_pct_of_debit
    ):
        return (
            f"profit_take_debit: pnl {pnl_pct_debit * 100:.1f}% of debit "
            f"(rule ≥ {rules.profit_take_pct_of_debit * 100:.0f}%)"
        )
    if (
        rules.loss_stop_pct_of_debit is not None
        and pnl_pct_debit <= -rules.loss_stop_pct_of_debit
    ):
        return (
            f"loss_stop_debit: pnl {pnl_pct_debit * 100:.1f}% of debit "
            f"(rule ≤ -{rules.loss_stop_pct_of_debit * 100:.0f}%)"
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
