"""SkewGatedStrategy — composes with any OptionStrategy to gate
entries on put-call skew percentile.

Practitioner thesis: when put-call skew is unusually STEEP (puts
much richer than calls), put credit spreads collect richer premium
AND the rare downside event is partially priced in. Conversely,
when skew is unusually FLAT (puts and calls similarly priced), the
trade is structurally less attractive.

Symmetric: a call credit spread benefits from skew being FLAT or
inverted (calls richer than puts), which historically happens
during stress regimes when option markets are pricing in upside
catastrophe scenarios.

Differs from `IvGatedStrategy` (which gates on overall IV level)
by gating on the SHAPE of the vol surface — uses the
`put_call_skew_25d` chain feature.

Stateful internally — maintains a rolling list of put-call skew
values per snap to compute the rank. Like `IvGatedStrategy`, the
runner sees a stateless ABC.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from tradegy.options.chain import ChainSnapshot
from tradegy.options.chain_features import put_call_skew_25d
from tradegy.options.positions import MultiLegOrder, MultiLegPosition
from tradegy.options.strategy import OptionStrategy


@dataclass
class SkewGatedStrategy(OptionStrategy):
    """Wrap `base` with an entry gate on rolling put-call skew rank.

    Min/max thresholds are inclusive. Setting only `min_skew_rank`
    is the canonical "sell put credit when skew is rich" gate.
    Setting only `max_skew_rank` would only fire when skew is FLAT
    (atypical for SPX).

    `target_dte` selects the expiry whose skew is measured —
    typically aligned with the wrapped strategy's target_dte.

    `window_days` controls the rolling rank window. 252 is
    canonical-1-year. The rank is computed as
    (current - window_min) / (window_max - window_min) ∈ [0, 1].
    """

    base: OptionStrategy
    min_skew_rank: float | None = None
    max_skew_rank: float | None = None
    target_dte: int = 30
    window_days: int = 252
    min_history_days: int | None = None
    id: str = ""

    _skew_history: list[float] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.id:
            parts = ["skew_gated"]
            if self.min_skew_rank is not None:
                parts.append(f"min{self.min_skew_rank:.2f}")
            if self.max_skew_rank is not None:
                parts.append(f"max{self.max_skew_rank:.2f}")
            parts.append(self.base.id)
            self.id = "_".join(parts)
        if self.min_history_days is None:
            self.min_history_days = self.window_days

    def on_chain(
        self,
        snapshot: ChainSnapshot,
        open_positions: tuple[MultiLegPosition, ...],
    ) -> MultiLegOrder | None:
        cur_skew = put_call_skew_25d(snapshot, target_dte=self.target_dte)
        if cur_skew != cur_skew:  # NaN
            return None
        self._skew_history.append(cur_skew)
        if len(self._skew_history) < self.min_history_days:
            return None

        window = self._skew_history[-self.window_days:]
        rank = self._compute_rank(cur_skew, window)
        if rank is None:
            return None

        if self.min_skew_rank is not None and rank < self.min_skew_rank:
            return None
        if self.max_skew_rank is not None and rank > self.max_skew_rank:
            return None

        return self.base.on_chain(snapshot, open_positions)

    @staticmethod
    def _compute_rank(value: float, window: list[float]) -> float | None:
        if not window:
            return None
        wmin = min(window)
        wmax = max(window)
        if wmax <= wmin:
            return None
        return (value - wmin) / (wmax - wmin)
