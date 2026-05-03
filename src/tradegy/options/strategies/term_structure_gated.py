"""TermStructureGatedStrategy — gates on the front-vs-far ATM IV
slope (contango vs backwardation).

Term structure semantics:
  Contango (typical): far ATM IV > near ATM IV. Slope NEGATIVE
                      under our convention `slope = near - far`.
                      Stable regime — short-vol-friendly.
  Backwardation (stress): near ATM IV > far ATM IV. Slope POSITIVE.
                          Vol-spike regime — short-vol-DANGEROUS.

Practitioner thesis: vol-selling strategies should AVOID entering
during backwardation (slope > 0). The structure inverts because
the market expects near-term turbulence. A short put entered into
a vol spike often gets pinned ITM before it can decay.

Default: `max_slope` = 0 (only enter when in contango — slope ≤ 0).
Setting `min_slope` would enter ONLY during backwardation — useful
for long-vol strategies (`ReverseIronCondor`), not for short-vol.

Stateless wrapper — current snap's term structure is computed
fresh each time. No history needed (unlike skew/IV rank which
need a rolling window).
"""
from __future__ import annotations

from dataclasses import dataclass

from tradegy.options.chain import ChainSnapshot
from tradegy.options.chain_features import term_structure_slope
from tradegy.options.positions import MultiLegOrder, MultiLegPosition
from tradegy.options.strategy import OptionStrategy


@dataclass(frozen=True)
class TermStructureGatedStrategy(OptionStrategy):
    """Wrap `base` with an entry gate on the live term-structure
    slope (near ATM IV minus far ATM IV).

    `max_slope`: only fire when slope ≤ this. Default None (no max).
    `min_slope`: only fire when slope ≥ this. Default None.

    For SHORT-vol strategies, set `max_slope = 0.0` to enter only
    in contango. For LONG-vol (e.g. ReverseIronCondor), set
    `min_slope = 0.0` to enter only in backwardation.
    """

    base: OptionStrategy
    min_slope: float | None = None
    max_slope: float | None = None
    near_dte: int = 30
    far_dte: int = 60
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            parts = ["ts_gated"]
            if self.min_slope is not None:
                parts.append(f"min{self.min_slope:+.3f}")
            if self.max_slope is not None:
                parts.append(f"max{self.max_slope:+.3f}")
            parts.append(self.base.id)
            object.__setattr__(self, "id", "_".join(parts))

    def on_chain(
        self,
        snapshot: ChainSnapshot,
        open_positions: tuple[MultiLegPosition, ...],
    ) -> MultiLegOrder | None:
        slope = term_structure_slope(
            snapshot, near_dte=self.near_dte, far_dte=self.far_dte,
        )
        if slope != slope:  # NaN
            return None
        if self.min_slope is not None and slope < self.min_slope:
            return None
        if self.max_slope is not None and slope > self.max_slope:
            return None
        return self.base.on_chain(snapshot, open_positions)
