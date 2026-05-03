"""Portfolio risk management for the multi-leg backtest runner.

Per `14_options_volatility_selling.md` Phase B-3 + the load-bearing
risk catalog (items 2 + 3 + 6 + 8): capital reservation,
concentration limits, tail-event halts, and underlying selection.

Design: strategies decide what to enter; the RiskManager decides
whether to allow it. The runner orchestrates. Strategies never
own risk decisions — that pattern is what makes the discipline
robust to a buggy or aggressive strategy class.

The 2026-05-03 B-2 real-data smoke test surfaced the exact failure
mode this module exists to prevent: IronCondor45dteD16 entered a
single position with $48,086 capital at risk against $25,000
target capital. Without the cap check below, a strategy could
silently 2x the user's intended exposure on day one.

Configuration (RiskConfig):

    declared_capital            $ envelope the user has authorized
    max_capital_at_risk_pct     fraction of declared_capital that
                                may be deployed at once
                                (e.g. 0.50 = at most 50%)
    max_per_expiration_pct      fraction of declared_capital that
                                may sit in any single expiration
                                cycle (e.g. 0.25 = max 25%)
    suspend_above_rv_pct        suspend new entries when the
                                trailing realized-vol percentile
                                exceeds this value
                                (e.g. 0.95 = halt when RV is in
                                top 5%)
    rv_window_days              window for realized vol computation
    rv_history_days             percentile-rank window
    min_history_for_rv_halt     skip RV halt check when fewer than
                                this many snapshots are available

Decisions (RiskDecision):

    approved        binary: open the position or not
    reason          short string for the audit trail
    contracts_authorized
                    contracts the runner should actually open
                    (currently equals the order's contracts when
                    approved; future revision may downsize)

Portfolio Greeks (PortfolioGreeks):

    Aggregate dollar exposure per Greek across all open positions.
    Useful for monitoring; not currently a gate (deferred until
    we have real-data evidence that monitoring it changes
    decisions).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from tradegy.options.chain import ChainSnapshot, OptionSide
from tradegy.options.chain_features import _atm_iv_series, realized_vol_30d
from tradegy.options.greeks import bs_greeks
from tradegy.options.positions import MultiLegPosition


# ── Config + decisions ────────────────────────────────────────────


@dataclass(frozen=True)
class RiskConfig:
    declared_capital: float
    max_capital_at_risk_pct: float = 0.50
    max_per_expiration_pct: float = 0.25
    suspend_above_rv_pct: float = 0.95
    rv_window_days: int = 30
    rv_history_days: int = 252
    min_history_for_rv_halt: int = 63

    @property
    def max_capital_at_risk_dollars(self) -> float:
        return self.declared_capital * self.max_capital_at_risk_pct

    @property
    def max_per_expiration_dollars(self) -> float:
        return self.declared_capital * self.max_per_expiration_pct


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    contracts_authorized: int = 0
    proposed_total_capital_at_risk: float = 0.0
    proposed_per_expiration_capital: float = 0.0
    realized_vol_percentile: float | None = None


# ── Portfolio Greeks ──────────────────────────────────────────────


@dataclass(frozen=True)
class PortfolioGreeks:
    """Aggregate dollar Greek exposure across positions.

    Sign conventions (consistent with bs_greeks per-share):
      delta_dollars    = sum_legs(qty * contracts * mult * delta_per_share * S)
                         signed: positive = long-the-underlying exposure.
      gamma_dollars    = sum_legs(qty * contracts * mult * gamma_per_share * S^2 * 0.01)
                         dollar P&L from a 1% move in underlying.
      theta_dollars    = sum_legs(qty * contracts * mult * theta_per_share / 365)
                         dollar decay per calendar day.
      vega_dollars     = sum_legs(qty * contracts * mult * vega_per_share / 100)
                         dollar P&L from a 1-vol-point move in IV.

    These mirror the trader-unit conventions ORATS publishes; we
    derive them from our textbook-units bs_greeks via the same
    /100, /365 conversions documented in tests/test_options_greeks.
    """

    delta_dollars: float = 0.0
    gamma_dollars: float = 0.0
    theta_dollars: float = 0.0
    vega_dollars: float = 0.0


def compute_portfolio_greeks(
    positions: Sequence[MultiLegPosition], snapshot: ChainSnapshot,
) -> PortfolioGreeks:
    """Aggregate Greek dollar exposure across `positions` at `snapshot`."""
    if not positions:
        return PortfolioGreeks()
    delta = gamma = theta = vega = 0.0
    snap_date = snapshot.ts_utc.date()
    S = snapshot.underlying_price
    r = snapshot.risk_free_rate

    # Build a fast lookup of (expiry, strike, side) → leg IV from
    # the snapshot. Reused per leg below.
    chain_iv: dict[tuple, float] = {}
    for leg in snapshot.legs:
        chain_iv[(leg.expiry, leg.strike, leg.side)] = leg.iv

    for pos in positions:
        if not pos.open or not pos.legs:
            continue
        mult = pos.legs[0].multiplier
        for leg in pos.legs:
            iv = chain_iv.get((leg.expiry, leg.strike, leg.side))
            if iv is None or iv <= 0:
                continue
            T = (leg.expiry - snap_date).days / 365.0
            if T <= 0:
                continue
            g = bs_greeks(
                S=S, K=leg.strike, T=T, r=r, sigma=iv, side=leg.side,
            )
            scale = leg.quantity * pos.contracts * mult
            delta += scale * g.delta * S
            gamma += scale * g.gamma * (S ** 2) * 0.01
            theta += scale * g.theta / 365.0
            vega += scale * g.vega / 100.0
    return PortfolioGreeks(
        delta_dollars=delta,
        gamma_dollars=gamma,
        theta_dollars=theta,
        vega_dollars=vega,
    )


# ── Risk manager ──────────────────────────────────────────────────


class RiskManager:
    """Evaluates each candidate position against capital + concentration
    + tail-event gates per `RiskConfig`.

    Stateless aside from the config — the runner provides current
    state on every call (open positions, snapshot history). This
    means a rejected order can be re-attempted on a future snapshot
    without any cleanup; the manager re-evaluates from scratch.
    """

    def __init__(self, config: RiskConfig) -> None:
        self._config = config

    @property
    def config(self) -> RiskConfig:
        return self._config

    def evaluate_order(
        self,
        *,
        proposed_position: MultiLegPosition,
        open_positions: Sequence[MultiLegPosition],
        snapshot_history: Sequence[ChainSnapshot],
    ) -> RiskDecision:
        """Return approve/reject for a candidate position.

        Checks in priority order: capital cap → per-expiration
        concentration → tail-event halt. First-fail wins so the
        operator gets the most-load-bearing reason in the audit
        trail.
        """
        cfg = self._config
        proposed_capital = proposed_position.total_capital_at_risk
        existing_capital = sum(
            p.total_capital_at_risk for p in open_positions if p.open
        )
        total_after = proposed_capital + existing_capital

        # Per-expiration cap. The proposed position may span multiple
        # expiries (calendar / diagonal); we charge each expiry the
        # full position max-loss (conservative — both legs of a
        # calendar share max-loss exposure, but for capital reserve
        # we'd rather over-budget than under).
        per_expiry_existing: dict = {}
        for p in open_positions:
            if not p.open:
                continue
            for e in p.expiries:
                per_expiry_existing[e] = (
                    per_expiry_existing.get(e, 0.0) + p.total_capital_at_risk
                )
        proposed_per_expiry_max = 0.0
        for e in proposed_position.expiries:
            after = per_expiry_existing.get(e, 0.0) + proposed_capital
            if after > proposed_per_expiry_max:
                proposed_per_expiry_max = after

        # Tail-event RV percentile.
        rv_pct = self._realized_vol_percentile(snapshot_history)

        # 1. Capital cap.
        if total_after > cfg.max_capital_at_risk_dollars:
            return RiskDecision(
                approved=False,
                reason=(
                    f"capital_cap: proposed total at risk "
                    f"${total_after:,.0f} exceeds "
                    f"{cfg.max_capital_at_risk_pct * 100:.0f}% of "
                    f"declared ${cfg.declared_capital:,.0f} "
                    f"(${cfg.max_capital_at_risk_dollars:,.0f})"
                ),
                proposed_total_capital_at_risk=total_after,
                proposed_per_expiration_capital=proposed_per_expiry_max,
                realized_vol_percentile=rv_pct,
            )

        # 2. Per-expiration concentration.
        if proposed_per_expiry_max > cfg.max_per_expiration_dollars:
            return RiskDecision(
                approved=False,
                reason=(
                    f"per_expiration_cap: ${proposed_per_expiry_max:,.0f} "
                    f"in one expiration cycle exceeds "
                    f"{cfg.max_per_expiration_pct * 100:.0f}% of "
                    f"declared (${cfg.max_per_expiration_dollars:,.0f})"
                ),
                proposed_total_capital_at_risk=total_after,
                proposed_per_expiration_capital=proposed_per_expiry_max,
                realized_vol_percentile=rv_pct,
            )

        # 3. Tail-event halt.
        if (
            rv_pct is not None
            and rv_pct >= cfg.suspend_above_rv_pct
        ):
            return RiskDecision(
                approved=False,
                reason=(
                    f"tail_event_halt: realized vol at "
                    f"p{rv_pct * 100:.1f} of trailing "
                    f"{cfg.rv_history_days} days exceeds "
                    f"halt threshold p{cfg.suspend_above_rv_pct * 100:.0f}"
                ),
                proposed_total_capital_at_risk=total_after,
                proposed_per_expiration_capital=proposed_per_expiry_max,
                realized_vol_percentile=rv_pct,
            )

        return RiskDecision(
            approved=True,
            reason="approved",
            contracts_authorized=proposed_position.contracts,
            proposed_total_capital_at_risk=total_after,
            proposed_per_expiration_capital=proposed_per_expiry_max,
            realized_vol_percentile=rv_pct,
        )

    # ── Internals ──────────────────────────────────────────────

    def _realized_vol_percentile(
        self, history: Sequence[ChainSnapshot],
    ) -> float | None:
        """Trailing realized-vol percentile from snapshot history.

        Returns None when fewer than `min_history_for_rv_halt`
        snapshots are available (insufficient data → don't halt;
        operator gets a clean default rather than a NaN-driven
        false positive on day 1 of a backtest).
        """
        cfg = self._config
        if len(history) < cfg.min_history_for_rv_halt:
            return None
        rv_df = realized_vol_30d(
            list(history),
            window_days=cfg.rv_window_days,
            annualization_days=252,
        )
        rv_values = [
            v for v in rv_df.get_column("realized_vol").to_list()
            if v is not None and v == v
        ]
        if not rv_values:
            return None
        # Window the percentile rank over the trailing
        # rv_history_days values.
        window = rv_values[-cfg.rv_history_days:]
        if not window:
            return None
        current = window[-1]
        less = sum(1 for v in window if v < current)
        return less / len(window)
