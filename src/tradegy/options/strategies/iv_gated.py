"""IV-gated strategy wrapper.

Composes any `OptionStrategy` with a regime gate: forward `on_chain`
calls only when the current ATM IV's rank in the trailing window
sits inside `[min_iv_rank, max_iv_rank]`.

Practitioner usage: vol selling fundamentally bets on
mean-reversion of IV. The trade tends to work when IV is HIGH
(option premiums richer, decay-to-realized advantage steeper) and
fail when IV is LOW (premiums thin, fewer wins to absorb the
occasional max loss). The standard tastytrade gate is "IV rank ≥
50" — sell vol only when the implied vol is at or above the median
of the trailing 252 trading days.

Symmetric upper bound: doc 14 risk-catalog item 2 (tail-event
protocol) — when IV rank is in the top 5%, halt new entries. The
RiskManager has its own version of this; the IV gate inside the
strategy is the FIRST line of defense, the runner's RiskManager
is the SECOND.

Design:

  - The wrapper IS-A OptionStrategy (composes via the ABC, so the
    runner sees a normal strategy and doesn't need to know about
    gating).
  - Stateful internally: maintains a rolling list of ATM IVs from
    every snap it has seen. Computes percentile rank of current
    snap's ATM IV inside the trailing `window_days`.
  - Wrapped strategy is stateless on each call (the wrapper makes
    the gating decision before delegating).
  - `id` derived from the base strategy's id + the gating
    parameters so the audit trail records what gate produced what
    trade (e.g. `iv_gated_min0.5_max0.95_put_credit_spread_45dte_d30`).

The wrapper does NOT cache the wrapped strategy's positions —
those still live in the runner. It only caches its own ATM IV
history for the rolling rank calculation.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from tradegy.options.chain import ChainSnapshot
from tradegy.options.chain_features import atm_iv
from tradegy.options.positions import MultiLegOrder, MultiLegPosition
from tradegy.options.strategy import OptionStrategy


@dataclass
class IvGatedStrategy(OptionStrategy):
    """Wrap `base` strategy with an IV-rank entry gate.

    Min/max IV-rank thresholds are inclusive. Both default to None
    (no gate); set one or both to enable gating. Setting only
    `min_iv_rank` is the canonical "sell vol when premium is rich"
    gate. Adding `max_iv_rank` adds a tail-event halt.

    `target_dte` selects the expiry for ATM-IV computation; should
    typically match the base strategy's target_dte so the gate
    measures the same vol the strategy will trade against. Default
    30 is a reasonable proxy for any strategy in the 30-45 DTE
    range.

    `window_days` sets the rolling rank window. 252 trading days
    is the canonical "1-year IV rank." Smaller windows (e.g. 63
    for 3 months) produce more responsive but noisier ranks.

    `min_history_days` gates the wrapper itself: until this many
    snaps have been seen, the wrapper returns None (cannot rank
    insufficient history). 252 by default — matches the rank
    window. A backtest needs at least one full window of history
    BEFORE the gate becomes informative.
    """

    base: OptionStrategy
    min_iv_rank: float | None = None
    max_iv_rank: float | None = None
    target_dte: int = 30
    window_days: int = 252
    min_history_days: int | None = None
    id: str = ""

    # Internal state — populated as the runner steps through snaps.
    _atm_iv_history: list[float] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        # Auto-derive id from base + gating params.
        if not self.id:
            parts = ["iv_gated"]
            if self.min_iv_rank is not None:
                parts.append(f"min{self.min_iv_rank:.2f}")
            if self.max_iv_rank is not None:
                parts.append(f"max{self.max_iv_rank:.2f}")
            parts.append(self.base.id)
            self.id = "_".join(parts)
        # min_history_days defaults to window_days (need full window
        # before rank is meaningful).
        if self.min_history_days is None:
            self.min_history_days = self.window_days

    def on_chain(
        self,
        snapshot: ChainSnapshot,
        open_positions: tuple[MultiLegPosition, ...],
    ) -> MultiLegOrder | None:
        # Compute current ATM IV; append to history. Even when
        # gating skips (insufficient history / out-of-range), we
        # still append so subsequent snaps see this one.
        cur_iv = atm_iv(snapshot, target_dte=self.target_dte)
        if cur_iv != cur_iv:  # NaN — chain too sparse for ATM
            return None
        self._atm_iv_history.append(cur_iv)

        if len(self._atm_iv_history) < self.min_history_days:
            return None

        window = self._atm_iv_history[-self.window_days:]
        rank = self._compute_rank(cur_iv, window)
        if rank is None:
            return None

        # Gate checks.
        if self.min_iv_rank is not None and rank < self.min_iv_rank:
            return None
        if self.max_iv_rank is not None and rank > self.max_iv_rank:
            return None

        order = self.base.on_chain(snapshot, open_positions)
        if order is None:
            return None
        # Re-tag with the wrapper's id so the portfolio runner /
        # closed-trade audit attribute the trade to the GATED
        # config, not the bare base. Without this re-tagging the
        # portfolio runner keys per_strategy by self.id but
        # _close_position looks up pos.strategy_class which is the
        # order tag → KeyError.
        from dataclasses import replace
        return replace(order, tag=self.id)

    @staticmethod
    def _compute_rank(value: float, window: list[float]) -> float | None:
        """Rank in [0, 1]. (current - window_min) / (window_max -
        window_min). Returns None when the window is degenerate
        (zero range or empty).
        """
        if not window:
            return None
        wmin = min(window)
        wmax = max(window)
        if wmax <= wmin:
            return None
        return (value - wmin) / (wmax - wmin)
