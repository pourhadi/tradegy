"""Chain-snapshot feature transforms — vol-selling decision inputs.

These are the per-snapshot scalar features the vol-selling strategy
classes condition entries on. They take ChainSnapshot(s) as input
and return either a single scalar (snapshot-level features) or a
DataFrame keyed by ts_utc (time-series features).

Catalog:

  ATM IV    — `atm_iv(snap, target_dte)`
              Implied vol at the strike closest to spot, averaged
              across call+put mid-IV. Foundation for everything
              else; vol-selling strategies fundamentally key on this.

  Term structure
              `term_structure_slope(snap, near_dte, far_dte)`
              Difference between near-month and far-month ATM IV.
              Negative typically (contango — far IVs higher); when
              positive (backwardation — near IVs higher), front-month
              vol-selling is more attractive.

  Skew      `put_call_skew_25d(snap, target_dte)`
              25-delta put IV minus 25-delta call IV at the chosen
              expiry. SPX is heavily put-skewed; this measures the
              demand for downside insurance vs upside speculation.

  Expected move
              `expected_move_to_expiry(snap, target_dte)`
              ATM straddle credit / underlying price. Approximates
              the 1-SD move between now and expiry as priced by the
              market. Strategy classes use this to pick wing widths.

  IV rank   `iv_rank_252d(snapshots)`
              Per-snapshot, where today's ATM IV sits in the trailing
              252 trading days' range:  (current - min) / (max - min)
              ∈ [0, 1]. tastytrade's canonical regime gate — sell vol
              when IV rank > 50.

  IV percentile
              `iv_percentile_252d(snapshots)`
              Same window but as percentile rank (fraction of
              historical values below current). More stable than IV
              rank when an outlier dominates the range.

  Realized vol
              `realized_vol_30d(snapshots)`
              Trailing 30-day annualized realized volatility from the
              underlying-price series implied by the snapshots. Sets
              the IV-RV spread that vol selling fundamentally trades.

All functions are pure — no I/O, no parquet writes. Higher-level
materialization (caching to disk, registry integration) lives in a
follow-up wrapper. This module focuses on getting the math right
against real ORATS data.

The `target_dte` argument throughout selects the expiry whose DTE is
closest to the requested value (default 30). When two expiries tie,
the one with the later expiry date wins.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from dataclasses import dataclass

import polars as pl

from tradegy.options.chain import ChainSnapshot, OptionLeg, OptionSide


# ── Helpers ───────────────────────────────────────────────────────


def _pick_expiry(snap: ChainSnapshot, target_dte: int) -> date:
    """Return the expiry whose DTE is closest to `target_dte`. Ties
    broken by preferring the later expiry (later DTE = more time-value
    headroom for management).
    """
    snap_date = snap.ts_utc.date()
    expiries = snap.expiries()
    if not expiries:
        raise ValueError(f"snapshot at {snap.ts_utc} has no expiries")
    best = expiries[0]
    best_dist = abs((best - snap_date).days - target_dte)
    for e in expiries[1:]:
        d = (e - snap_date).days
        dist = abs(d - target_dte)
        if dist < best_dist or (dist == best_dist and e > best):
            best = e
            best_dist = dist
    return best


def _legs_at(snap: ChainSnapshot, expiry: date, side: OptionSide) -> list[OptionLeg]:
    """All legs for one (expiry, side) sorted by strike ascending,
    filtered to "fillable" legs (positive iv + positive bid+ask).
    """
    out = [
        l for l in snap.for_expiry(expiry)
        if l.side == side and l.iv > 0.0 and (l.bid > 0.0 or l.ask > 0.0)
    ]
    out.sort(key=lambda l: l.strike)
    return out


def _atm_strike(legs: list[OptionLeg], underlying: float) -> OptionLeg | None:
    if not legs:
        return None
    return min(legs, key=lambda l: abs(l.strike - underlying))


# ── Snapshot-level features ───────────────────────────────────────


def atm_iv(snap: ChainSnapshot, *, target_dte: int = 30) -> float:
    """ATM IV at the chosen DTE: average of call and put mid-IV at
    the strike closest to spot. NaN if either side has no fillable
    leg.
    """
    expiry = _pick_expiry(snap, target_dte)
    calls = _legs_at(snap, expiry, OptionSide.CALL)
    puts = _legs_at(snap, expiry, OptionSide.PUT)
    atm_call = _atm_strike(calls, snap.underlying_price)
    atm_put = _atm_strike(puts, snap.underlying_price)
    if atm_call is None or atm_put is None:
        return float("nan")
    return 0.5 * (atm_call.iv + atm_put.iv)


def term_structure_slope(
    snap: ChainSnapshot, *, near_dte: int = 30, far_dte: int = 60,
) -> float:
    """Near-month ATM IV minus far-month ATM IV.

    Sign convention: NEAR - FAR. Negative = contango (far higher,
    typical), positive = backwardation (near higher, stress regime).
    Magnitude is in absolute IV units (e.g. -0.03 = far is 3 vol
    points higher).
    """
    near = atm_iv(snap, target_dte=near_dte)
    far = atm_iv(snap, target_dte=far_dte)
    if math.isnan(near) or math.isnan(far):
        return float("nan")
    return near - far


def put_call_skew_25d(
    snap: ChainSnapshot, *, target_dte: int = 30,
) -> float:
    """25-delta put IV minus 25-delta call IV at the chosen expiry.

    Computed by finding the call leg whose Black-Scholes delta is
    closest to +0.25 and the put leg whose delta is closest to
    -0.25, both at the same expiry, and returning put_iv - call_iv.

    SPX runs heavily positive (put IV > call IV) due to portfolio-
    insurance demand. NaN if either leg can't be located (sparse
    chain at this DTE).
    """
    from tradegy.options.greeks import bs_greeks  # avoid cycle

    expiry = _pick_expiry(snap, target_dte)
    snap_date = snap.ts_utc.date()
    T = (expiry - snap_date).days / 365.0
    if T <= 0:
        return float("nan")

    calls = _legs_at(snap, expiry, OptionSide.CALL)
    puts = _legs_at(snap, expiry, OptionSide.PUT)
    if not calls or not puts:
        return float("nan")

    def _delta_for(leg: OptionLeg) -> float:
        g = bs_greeks(
            S=snap.underlying_price, K=leg.strike, T=T,
            r=snap.risk_free_rate, sigma=leg.iv, side=leg.side,
        )
        return g.delta

    call_25 = min(calls, key=lambda l: abs(_delta_for(l) - 0.25))
    put_25 = min(puts, key=lambda l: abs(_delta_for(l) + 0.25))
    return put_25.iv - call_25.iv


def expected_move_to_expiry(
    snap: ChainSnapshot, *, target_dte: int = 30,
) -> float:
    """1-SD expected move as priced by the ATM straddle.

    Returns straddle_credit / underlying_price. For SPX at 30 DTE
    with ATM IV ~13% this is roughly 0.04 (4%); for the same DTE in
    a stress regime with IV at 30% it's roughly 0.10 (10%). Used by
    strategy classes to anchor wing widths and short-strike
    placement.
    """
    expiry = _pick_expiry(snap, target_dte)
    calls = _legs_at(snap, expiry, OptionSide.CALL)
    puts = _legs_at(snap, expiry, OptionSide.PUT)
    atm_call = _atm_strike(calls, snap.underlying_price)
    atm_put = _atm_strike(puts, snap.underlying_price)
    if atm_call is None or atm_put is None:
        return float("nan")
    if snap.underlying_price <= 0:
        return float("nan")
    straddle = atm_call.mid + atm_put.mid
    return straddle / snap.underlying_price


# ── Time-series features ──────────────────────────────────────────


def _atm_iv_series(
    snapshots: list[ChainSnapshot], *, target_dte: int = 30,
) -> pl.DataFrame:
    """Build a per-snapshot ATM IV series. Helper for IV-rank /
    IV-percentile / realized-vol features.
    """
    rows = [
        {"ts_utc": s.ts_utc, "atm_iv": atm_iv(s, target_dte=target_dte)}
        for s in snapshots
    ]
    return pl.DataFrame(rows).sort("ts_utc")


def iv_rank_252d(
    snapshots: list[ChainSnapshot], *, target_dte: int = 30,
    window_days: int = 252,
) -> pl.DataFrame:
    """Per-snapshot IV rank in the trailing `window_days` of ATM IVs.

    Rank = (current - window_min) / (window_max - window_min) ∈ [0, 1].
    Returns DataFrame with columns: ts_utc, atm_iv, iv_rank.

    Snapshots earlier than `window_days` deep get iv_rank=NaN
    (insufficient history). The first eligible snapshot uses
    snapshots[0..window_days] inclusive of itself.
    """
    series = _atm_iv_series(snapshots, target_dte=target_dte)
    if series.is_empty():
        return series.with_columns(pl.lit(float("nan")).alias("iv_rank"))

    # Polars rolling: include current row; for snapshots earlier than
    # window_days, min_samples=window_days makes the result null.
    out = series.with_columns([
        pl.col("atm_iv")
        .rolling_min(window_size=window_days, min_samples=window_days)
        .alias("_window_min"),
        pl.col("atm_iv")
        .rolling_max(window_size=window_days, min_samples=window_days)
        .alias("_window_max"),
    ])
    out = out.with_columns(
        pl.when(pl.col("_window_max") > pl.col("_window_min"))
        .then(
            (pl.col("atm_iv") - pl.col("_window_min"))
            / (pl.col("_window_max") - pl.col("_window_min"))
        )
        .otherwise(float("nan"))
        .alias("iv_rank")
    ).drop(["_window_min", "_window_max"])
    return out


def iv_percentile_252d(
    snapshots: list[ChainSnapshot], *, target_dte: int = 30,
    window_days: int = 252,
) -> pl.DataFrame:
    """Per-snapshot IV percentile: fraction of trailing-window ATM IVs
    strictly less than current.

    More robust than IV rank when one outlier dominates the range
    (rank goes to ~0 the day after a vol crash; percentile is more
    gradual). Returns columns: ts_utc, atm_iv, iv_percentile.
    """
    series = _atm_iv_series(snapshots, target_dte=target_dte)
    if series.is_empty():
        return series.with_columns(pl.lit(float("nan")).alias("iv_percentile"))

    values = series.get_column("atm_iv").to_list()
    n = len(values)
    out_pct: list[float] = []
    for i in range(n):
        if i + 1 < window_days:
            out_pct.append(float("nan"))
            continue
        window = values[i + 1 - window_days : i + 1]  # inclusive of current
        cur = values[i]
        if cur != cur:  # NaN guard
            out_pct.append(float("nan"))
            continue
        valid = [v for v in window if v == v]
        if not valid:
            out_pct.append(float("nan"))
            continue
        less = sum(1 for v in valid if v < cur)
        out_pct.append(less / len(valid))
    return series.with_columns(
        pl.Series("iv_percentile", out_pct, dtype=pl.Float64)
    )


def realized_vol_30d(
    snapshots: list[ChainSnapshot], *, window_days: int = 30,
    annualization_days: int = 252,
) -> pl.DataFrame:
    """Per-snapshot trailing realized volatility from the underlying-
    price series implied by the snapshots.

    Computed as stdev(log returns over window) * sqrt(annualization).
    Returns columns: ts_utc, underlying_price, realized_vol.

    Snapshots earlier than `window_days+1` deep get realized_vol=NaN
    (need at least window_days log returns).
    """
    rows = [
        {"ts_utc": s.ts_utc, "underlying_price": s.underlying_price}
        for s in snapshots
    ]
    df = pl.DataFrame(rows).sort("ts_utc")
    if df.height < 2:
        return df.with_columns(pl.lit(float("nan")).alias("realized_vol"))

    df = df.with_columns(
        (pl.col("underlying_price") / pl.col("underlying_price").shift(1))
        .log()
        .alias("_logret")
    )
    df = df.with_columns(
        pl.col("_logret")
        .rolling_std(window_size=window_days, min_samples=window_days)
        .alias("_rv_period")
    )
    df = df.with_columns(
        (pl.col("_rv_period") * math.sqrt(annualization_days))
        .alias("realized_vol")
    )
    return df.drop(["_logret", "_rv_period"])
