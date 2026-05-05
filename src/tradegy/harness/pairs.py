"""Pairs-trading backtest runner.

Self-contained from the single-leg harness in runner.py — keeping it
separate so the existing 608-test single-leg infrastructure stays
untouched. This module can be folded into the main harness once the
pairs mechanism is validated.

Mechanic:
  Two legs (A, B) traded simultaneously as a pair. When the entry
  signal fires (typically a z-score deviation in their price ratio),
  both legs are opened with their respective notionals. When the exit
  signal fires (z-score reverts toward zero), both close. P&L is
  computed as the dollar-neutral spread P&L:
    pair_pnl = leg_a_pnl + leg_b_pnl
  where leg_b is sized to be approximately dollar-neutral with leg_a
  at entry (e.g., 1 MES contract @ $27.5K notional <-> 50 SPY shares
  @ $27.5K notional).

What this module does NOT do (deferred until validated):
  - Multiple concurrent pairs
  - Non-fixed hedge ratios (no live beta-adjustment)
  - Stop-loss on the pair (only signal-driven exit + time stop)
  - Per-leg slippage modeling beyond simple fixed cost
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from pathlib import Path
from typing import Iterator

import polars as pl

from tradegy.harness.data import get_feature, load_bar_stream
from tradegy.harness.execution import CostModel


class PairSide(Enum):
    LONG_A_SHORT_B = "long_a_short_b"
    SHORT_A_LONG_B = "short_a_long_b"


@dataclass
class PairTrade:
    side: PairSide
    entry_ts: datetime
    exit_ts: datetime
    entry_price_a: float
    entry_price_b: float
    exit_price_a: float
    exit_price_b: float
    n_a: int
    n_b: int
    multiplier_a: float  # $ per point of price for instrument A (e.g., MES = 5)
    multiplier_b: float  # for instrument B (e.g., SPY share = 1)
    leg_a_pnl: float
    leg_b_pnl: float
    cost: float
    bars_held: int

    @property
    def total_pnl(self) -> float:
        return self.leg_a_pnl + self.leg_b_pnl - self.cost


@dataclass
class PairsConfig:
    pair_id: str
    leg_a_instrument: str
    leg_b_instrument: str
    n_a: int
    n_b: int
    multiplier_a: float
    multiplier_b: float
    spread_zscore_feature: str
    entry_threshold: float = 2.0
    exit_threshold: float = 0.5
    side: PairSide = PairSide.SHORT_A_LONG_B
    rth_only: bool = True
    rth_session_feature: str | None = None
    rth_lo: float = 0.05
    rth_hi: float = 0.90
    max_holding_bars: int = 30
    use_zscore_exit: bool = True   # if False, only time-stop exit
    max_attempts_per_session: int = 5
    cost_per_trade: float = 3.50  # rough: MES round-trip $1.50 + SPY 50sh × $0.005 + slip


@dataclass
class PairsResult:
    config: PairsConfig
    trades: list[PairTrade]
    n_bars: int
    coverage_start: datetime | None
    coverage_end: datetime | None

    @property
    def total_pnl(self) -> float:
        return sum(t.total_pnl for t in self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.total_pnl > 0)
        return wins / len(self.trades)

    @property
    def avg_pnl(self) -> float:
        if not self.trades:
            return 0.0
        return self.total_pnl / len(self.trades)

    @property
    def per_trade_sharpe(self) -> float:
        if len(self.trades) < 2:
            return 0.0
        pnls = [t.total_pnl for t in self.trades]
        import statistics
        m = statistics.mean(pnls)
        s = statistics.pstdev(pnls)
        return m / s if s > 0 else 0.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.total_pnl for t in self.trades if t.total_pnl > 0)
        losses = sum(-t.total_pnl for t in self.trades if t.total_pnl < 0)
        if losses == 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    @property
    def avg_holding_bars(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.bars_held for t in self.trades) / len(self.trades)


def run_pairs_backtest(
    config: PairsConfig,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    feature_root: Path | None = None,
) -> PairsResult:
    bars_a = load_bar_stream(
        config.leg_a_instrument, bar_cadence="1m",
        start=start, end=end, feature_root=feature_root,
    )
    bars_b = load_bar_stream(
        config.leg_b_instrument, bar_cadence="1m",
        start=start, end=end, feature_root=feature_root,
    )
    if bars_a.height == 0 or bars_b.height == 0:
        return PairsResult(
            config=config, trades=[],
            n_bars=0, coverage_start=None, coverage_end=None,
        )

    zscore = get_feature(config.spread_zscore_feature, feature_root=feature_root)
    if "served_at" not in zscore.columns:
        zscore = zscore.with_columns(pl.col("ts_utc").alias("served_at"))
    zscore = zscore.with_columns(
        pl.col("ts_utc").cast(pl.Datetime("ns", "UTC")),
        pl.col("served_at").cast(pl.Datetime("ns", "UTC")),
        pl.col("value").alias("zscore"),
    ).select("ts_utc", "zscore", "served_at").sort("ts_utc")

    # Build the joined panel: bars_a + bars_b on ts_utc + zscore + RTH gate.
    panel = (
        bars_a.select(
            pl.col("ts_utc").cast(pl.Datetime("ns", "UTC")),
            pl.col("open").alias("a_open"),
            pl.col("high").alias("a_high"),
            pl.col("low").alias("a_low"),
            pl.col("close").alias("a_close"),
        )
        .join(
            bars_b.select(
                pl.col("ts_utc").cast(pl.Datetime("ns", "UTC")),
                pl.col("open").alias("b_open"),
                pl.col("high").alias("b_high"),
                pl.col("low").alias("b_low"),
                pl.col("close").alias("b_close"),
            ),
            on="ts_utc",
            how="inner",
        )
        .join(
            zscore.select("ts_utc", "zscore"),
            on="ts_utc",
            how="left",
        )
        .sort("ts_utc")
    )

    if config.rth_only:
        rth_id = config.rth_session_feature or (
            f"{config.leg_a_instrument.lower()}_xnys_session_position"
        )
        rth = get_feature(rth_id, feature_root=feature_root)
        rth = rth.with_columns(
            pl.col("ts_utc").cast(pl.Datetime("ns", "UTC")),
            pl.col("value").alias("session_pos"),
        ).select("ts_utc", "session_pos")
        panel = panel.join(rth, on="ts_utc", how="left")
    else:
        panel = panel.with_columns(pl.lit(0.5).alias("session_pos"))

    panel = panel.filter(pl.col("zscore").is_not_null())

    # State machine
    trades: list[PairTrade] = []
    in_position = False
    side: PairSide | None = None
    entry_ts: datetime | None = None
    entry_a_price: float = 0.0
    entry_b_price: float = 0.0
    bars_held = 0
    pending_entry: PairSide | None = None
    pending_exit = False
    last_session_date: str | None = None
    attempts_this_session = 0

    rows = list(panel.iter_rows(named=True))
    n_rows = len(rows)
    if not rows:
        return PairsResult(
            config=config, trades=[],
            n_bars=0, coverage_start=None, coverage_end=None,
        )

    for i, row in enumerate(rows):
        ts = row["ts_utc"]
        a_open = row["a_open"]
        a_close = row["a_close"]
        b_open = row["b_open"]
        b_close = row["b_close"]
        z = row["zscore"]
        session_pos = row["session_pos"]
        date_str = ts.date().isoformat()

        # Reset attempts at session boundary (UTC date change).
        if date_str != last_session_date:
            attempts_this_session = 0
            last_session_date = date_str

        # Fill pending entry at this bar's open.
        if pending_entry is not None and not in_position:
            in_position = True
            side = pending_entry
            entry_ts = ts
            entry_a_price = a_open
            entry_b_price = b_open
            bars_held = 0
            pending_entry = None
            attempts_this_session += 1

        # Fill pending exit at this bar's open.
        if pending_exit and in_position:
            exit_a_price = a_open
            exit_b_price = b_open
            # Compute leg P&L by side.
            if side == PairSide.LONG_A_SHORT_B:
                leg_a_pnl = (exit_a_price - entry_a_price) * config.n_a * config.multiplier_a
                leg_b_pnl = (entry_b_price - exit_b_price) * config.n_b * config.multiplier_b
            else:  # SHORT_A_LONG_B
                leg_a_pnl = (entry_a_price - exit_a_price) * config.n_a * config.multiplier_a
                leg_b_pnl = (exit_b_price - entry_b_price) * config.n_b * config.multiplier_b
            trades.append(PairTrade(
                side=side,
                entry_ts=entry_ts, exit_ts=ts,
                entry_price_a=entry_a_price, entry_price_b=entry_b_price,
                exit_price_a=exit_a_price, exit_price_b=exit_b_price,
                n_a=config.n_a, n_b=config.n_b,
                multiplier_a=config.multiplier_a, multiplier_b=config.multiplier_b,
                leg_a_pnl=leg_a_pnl, leg_b_pnl=leg_b_pnl,
                cost=config.cost_per_trade,
                bars_held=bars_held,
            ))
            in_position = False
            side = None
            pending_exit = False
            bars_held = 0

        if in_position:
            bars_held += 1
            # Time stop?
            if bars_held >= config.max_holding_bars:
                pending_exit = True
                continue
            # Mean-reversion exit signal: z-score crossed back through
            # threshold. Disabled when use_zscore_exit=False — then
            # only time stop closes the trade. The rolling z-score
            # update can fire premature exits via local oscillation
            # while the underlying ratio is still mean-reverting.
            if config.use_zscore_exit:
                if side == PairSide.LONG_A_SHORT_B:
                    if z is not None and z > -config.exit_threshold:
                        pending_exit = True
                        continue
                else:  # SHORT_A_LONG_B
                    if z is not None and z < config.exit_threshold:
                        pending_exit = True
                        continue
        else:
            # Look for entry signal.
            if attempts_this_session >= config.max_attempts_per_session:
                continue
            if config.rth_only:
                if session_pos is None:
                    continue
                if not (config.rth_lo <= session_pos <= config.rth_hi):
                    continue
            if z is None:
                continue
            # Match entry direction to configured side.
            if config.side == PairSide.LONG_A_SHORT_B:
                if z < -config.entry_threshold:
                    pending_entry = PairSide.LONG_A_SHORT_B
            else:
                if z > config.entry_threshold:
                    pending_entry = PairSide.SHORT_A_LONG_B

    # If still in position at end of data, close at last bar's close.
    if in_position:
        last = rows[-1]
        if side == PairSide.LONG_A_SHORT_B:
            leg_a_pnl = (last["a_close"] - entry_a_price) * config.n_a * config.multiplier_a
            leg_b_pnl = (entry_b_price - last["b_close"]) * config.n_b * config.multiplier_b
        else:
            leg_a_pnl = (entry_a_price - last["a_close"]) * config.n_a * config.multiplier_a
            leg_b_pnl = (last["b_close"] - entry_b_price) * config.n_b * config.multiplier_b
        trades.append(PairTrade(
            side=side,
            entry_ts=entry_ts, exit_ts=last["ts_utc"],
            entry_price_a=entry_a_price, entry_price_b=entry_b_price,
            exit_price_a=last["a_close"], exit_price_b=last["b_close"],
            n_a=config.n_a, n_b=config.n_b,
            multiplier_a=config.multiplier_a, multiplier_b=config.multiplier_b,
            leg_a_pnl=leg_a_pnl, leg_b_pnl=leg_b_pnl,
            cost=config.cost_per_trade,
            bars_held=bars_held,
        ))

    return PairsResult(
        config=config,
        trades=trades,
        n_bars=n_rows,
        coverage_start=rows[0]["ts_utc"],
        coverage_end=rows[-1]["ts_utc"],
    )
