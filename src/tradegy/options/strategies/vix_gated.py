"""VIX-daily regime-gated strategy wrapper.

Composes any `OptionStrategy` with a regime gate keyed on the prior
session's CBOE VIX cash close.  Used to test whether MES 0DTE
strategies have hidden edge in specific vol regimes.

Why VIX as the gate (and not chain IV):

  databento ohlcv-1m doesn't carry IV — the chain reader emits
  iv=0.0 sentinel on every leg.  The existing `IvGatedStrategy`
  wrapper computes IV rank from chain ATM IV, which on databento
  data evaluates to junk.  VIX is the canonical 30-day implied
  vol on SPX; for MES (which tracks the S&P 500 nearly 1:1) VIX
  is a perfectly serviceable regime indicator that we already
  have on disk in `vix_daily`.

No-lookahead.  The gate uses the PRIOR session's VIX close — the
value that was published at 16:00 ET on the day before the entry.
On the day of trade, VIX hasn't yet closed at the strategy's
14:30 UTC entry time, so using same-day VIX would peek.

Two threshold modes (set whichever combination matters):

  Absolute thresholds — `min_vix_close`, `max_vix_close`:
    Plain comparison against the prior-day VIX close in points.
    Useful for "VIX < 18" or "VIX > 25" rules.

  Percentile-rank thresholds — `min_vix_pctile_252`, `max_vix_pctile_252`:
    Compute the prior-day VIX's percentile rank within the trailing
    252-trading-day window.  Useful for "VIX in lowest quartile of
    last year" (mirroring the doc-14 path-1 finding for SPY 45DTE).

Both modes can stack — set absolute AND percentile thresholds and
the strategy enters only when ALL gates pass.

Internal state: the wrapper loads the entire VIX daily series once
at construction and caches an in-memory date→VIX map plus a sorted
array for percentile-rank lookups.  No runtime registry queries.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from tradegy.options.chain import ChainSnapshot
from tradegy.options.positions import MultiLegOrder, MultiLegPosition
from tradegy.options.strategy import OptionStrategy


def _load_vix_daily(root: Path | None = None) -> pl.DataFrame:
    """Load the entire VIX daily series sorted by ts_utc."""
    if root is None:
        from tradegy import config
        root = config.raw_dir()
    base = root / "source=vix_daily"
    if not base.exists():
        raise FileNotFoundError(f"vix_daily source not found at {base}")
    pattern = str(base / "date=*" / "data.parquet")
    df = pl.read_parquet(pattern).sort("ts_utc")
    return df.with_columns(pl.col("ts_utc").dt.date().alias("trade_date"))


def _prior_trading_day_vix(
    vix_df: pl.DataFrame, target: date,
) -> tuple[date, float] | None:
    """Find the most recent VIX close strictly BEFORE `target`.

    Returns (date, close) or None if no prior data exists.
    """
    prior = vix_df.filter(pl.col("trade_date") < target)
    if prior.height == 0:
        return None
    last = prior.tail(1).row(0, named=True)
    return last["trade_date"], float(last["close"])


def _same_session_vix(
    vix_df: pl.DataFrame, target: date, field: str,
) -> tuple[date, float] | None:
    """Look up `target`'s OWN VIX OHLC bar field (not the prior
    session's).  Used for the intraday-VIX gate variant — the VIX
    OPEN is published at 9:30 ET, an hour before our 10:30 entry,
    so it's a no-lookahead measurement of "vol at session start
    today" rather than "vol at the close yesterday".

    `field` must be one of: "open", "high", "low", "close".
    Returns (date, value) or None if `target` has no VIX bar.

    Note: for backtest cleanliness the ENTRY-time gate should use
    "open" (knowable at 10:30 entry).  "high" / "low" / "close"
    introduce lookahead — they're useful only for retrospective
    analysis or for management-time triggers.
    """
    if field not in ("open", "high", "low", "close"):
        raise ValueError(f"unknown VIX field {field!r}")
    same = vix_df.filter(pl.col("trade_date") == target)
    if same.height == 0:
        return None
    row = same.row(0, named=True)
    return row["trade_date"], float(row[field])


def _percentile_rank(
    sorted_values: list[float], x: float,
) -> float:
    """Return the percentile rank (0..1) of x within sorted_values.

    Uses bisect to find the insertion position; the rank is
    (position) / len.  No interpolation — rank 0.25 means "at or
    below the 25th percentile."
    """
    if not sorted_values:
        return 0.5  # default mid-rank if no history
    pos = bisect.bisect_right(sorted_values, x)
    return pos / len(sorted_values)


@dataclass
class VixGatedStrategy(OptionStrategy):
    """Wrap `base` strategy with a VIX-daily entry gate.

    Set any subset of the threshold parameters; all set thresholds
    must pass for the gate to allow the trade.  An unset threshold
    (None) is not enforced.

    `gate_source` controls where the VIX value comes from:

      "prior_close" (default): the most recent VIX close strictly
        BEFORE the entry session — yesterday's 16:00 ET print.
        No-lookahead by construction.  This is the OOS-validated
        spec.

      "today_open":  today's VIX OPEN (9:30 ET print) — known by
        the 10:30 ET entry time, no lookahead, but reflects the
        morning's actual vol level rather than yesterday's
        residual fear.  EXPERIMENTAL — needs separate validation.

    `min_vix_close` / `max_vix_close` are kept as parameter names
    for backward compatibility — they apply to whichever VIX value
    `gate_source` selects.
    """

    base: OptionStrategy
    min_vix_close: float | None = None
    max_vix_close: float | None = None
    min_vix_pctile_252: float | None = None
    max_vix_pctile_252: float | None = None
    gate_source: str = "prior_close"
    vix_root: Path | None = None
    id: str = ""

    _vix_df: pl.DataFrame = field(default=None, init=False, repr=False)
    _vix_dates_sorted: list[date] = field(default_factory=list, init=False, repr=False)
    _vix_closes_by_date: dict = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.gate_source not in ("prior_close", "today_open"):
            raise ValueError(
                f"unknown gate_source {self.gate_source!r} (use "
                f"'prior_close' or 'today_open')"
            )
        if not self.id:
            parts = ["vix_gated"]
            if self.gate_source != "prior_close":
                parts.append(self.gate_source)
            if self.min_vix_close is not None:
                parts.append(f"vixmin{self.min_vix_close}")
            if self.max_vix_close is not None:
                parts.append(f"vixmax{self.max_vix_close}")
            if self.min_vix_pctile_252 is not None:
                parts.append(f"pctmin{self.min_vix_pctile_252}")
            if self.max_vix_pctile_252 is not None:
                parts.append(f"pctmax{self.max_vix_pctile_252}")
            parts.append(self.base.id)
            self.id = "_".join(parts)

        # Load VIX once.
        df = _load_vix_daily(self.vix_root)
        self._vix_df = df
        self._vix_dates_sorted = df["trade_date"].to_list()
        self._vix_closes_by_date = {
            d: float(c)
            for d, c in zip(df["trade_date"].to_list(), df["close"].to_list())
        }

    def _gate_value_for(self, snap_date: date) -> tuple[date, float] | None:
        """Resolve the VIX value the gate compares against, per
        `gate_source` configuration.
        """
        if self.gate_source == "prior_close":
            return _prior_trading_day_vix(self._vix_df, snap_date)
        if self.gate_source == "today_open":
            # Same-session OPEN (9:30 ET print) — no lookahead at
            # the 10:30 ET entry time.
            return _same_session_vix(self._vix_df, snap_date, "open")
        raise ValueError(f"unknown gate_source {self.gate_source!r}")

    def _gate_passes(self, snap_date: date) -> bool:
        """Return True iff the configured VIX value passes all thresholds."""
        result = self._gate_value_for(snap_date)
        if result is None:
            return False  # insufficient history / no bar for snap_date
        ref_date, vix_val = result

        if self.min_vix_close is not None and vix_val < self.min_vix_close:
            return False
        if self.max_vix_close is not None and vix_val > self.max_vix_close:
            return False

        if (
            self.min_vix_pctile_252 is not None
            or self.max_vix_pctile_252 is not None
        ):
            # 252-day rolling window of VIX closes ending on ref_date.
            # NOTE: rank is always computed on prior CLOSES (the
            # canonical "VIX rank" definition), regardless of
            # gate_source — only the threshold-comparison field
            # changes between prior-close vs intraday OPEN.
            window_start = ref_date - timedelta(days=365)
            window = self._vix_df.filter(
                (pl.col("trade_date") >= window_start)
                & (pl.col("trade_date") <= ref_date)
            )
            sorted_window = sorted(window["close"].to_list())
            rank = _percentile_rank(sorted_window, vix_val)
            if (
                self.min_vix_pctile_252 is not None
                and rank < self.min_vix_pctile_252
            ):
                return False
            if (
                self.max_vix_pctile_252 is not None
                and rank > self.max_vix_pctile_252
            ):
                return False

        return True

    def on_chain(
        self,
        snapshot: ChainSnapshot,
        open_positions: tuple[MultiLegPosition, ...],
    ) -> MultiLegOrder | None:
        if not self._gate_passes(snapshot.ts_utc.date()):
            return None
        order = self.base.on_chain(snapshot, open_positions)
        if order is None:
            return None
        # Re-tag the order with the wrapper's id so audit trail
        # records that the gate fired.
        from tradegy.options.positions import MultiLegOrder as _MLO
        return _MLO(tag=self.id, contracts=order.contracts, legs=order.legs)
