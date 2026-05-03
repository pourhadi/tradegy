"""Chain-snapshot reader — parquet → typed ChainSnapshot objects.

Bridges the ORATS-ingested parquet partitions (one per trade_date,
flat schema with per-row strike/expiry) into the
`ChainSnapshot`/`OptionLeg` dataclasses that strategy classes
consume.

This is the chain-cadence analogue of `harness.data.load_bar_stream`
for bar streams. Both read from `data/raw/source=<id>/date=*/data.
parquet`; the difference is the per-snapshot grouping (a chain
snapshot owns many strikes, a bar owns one OHLCV row).

Per `14_options_volatility_selling.md` Phase A: ORATS-published
Greeks (`call_delta`, `put_delta`, etc.) are present in the parquet
but NOT propagated to `OptionLeg` instances — strategies recompute
via `tradegy.options.greeks.bs_greeks`. The vendor Greeks columns
remain accessible via the raw polars frame for cross-check audits.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import polars as pl

from tradegy import config
from tradegy.options.chain import (
    ChainSnapshot,
    OptionLeg,
    OptionSide,
)


# Cash-index option multipliers. SPX/NDX/RUT use the standard $100
# multiplier; XSP is the CBOE Mini-SPX product with $10 multiplier
# (1/10 size, retail-friendly). SPY is an ETF with $100 multiplier
# but each strike is ~1/10 of SPX's, so per-contract dollar exposure
# is naturally 1/10 SPX too.
# Futures-options multipliers (e.g. /ES = 50) are added when an
# /ES options source lands.
_DEFAULT_MULTIPLIER_BY_TICKER: dict[str, int] = {
    "SPX": 100,
    "XSP": 10,
    "SPY": 100,
    "NDX": 100,
    "RUT": 100,
}


def _raw_root(source_id: str, *, root: Path | None = None) -> Path:
    base = root or config.raw_dir()
    return base / f"source={source_id}"


def load_chain_frames(
    source_id: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    root: Path | None = None,
) -> pl.DataFrame:
    """Read all parquet partitions for a chain source as a single
    sorted DataFrame.

    Returns the canonical flat schema (one row per (ts_utc, expir,
    strike)) with both call and put leg columns side-by-side. Use
    `iter_chain_snapshots` to project this into per-snapshot
    `ChainSnapshot` objects.

    `start` / `end` filter on `ts_utc` inclusive.
    """
    base = _raw_root(source_id, root=root)
    if not base.exists():
        raise FileNotFoundError(
            f"no ingested chain data for source={source_id} at {base}"
        )
    pattern = str(base / "date=*" / "data.parquet")
    df = pl.read_parquet(pattern).sort(["ts_utc", "expir_date", "strike"])
    if start is not None:
        df = df.filter(pl.col("ts_utc") >= start)
    if end is not None:
        df = df.filter(pl.col("ts_utc") <= end)
    return df


def iter_chain_snapshots(
    source_id: str,
    *,
    ticker: str = "SPX",
    start: datetime | None = None,
    end: datetime | None = None,
    root: Path | None = None,
):
    """Yield one `ChainSnapshot` per trade date in the source.

    Iterator semantics intended — chain data for SPX over multiple
    years can be hundreds of MB once decoded; iterating one
    snapshot at a time keeps memory bounded for backtests. The
    iterator yields snapshots in ascending ts_utc order.
    """
    df = load_chain_frames(source_id, start=start, end=end, root=root)
    if df.height == 0:
        return
    multiplier = _DEFAULT_MULTIPLIER_BY_TICKER.get(ticker, 100)

    # Group by ts_utc (one snapshot per session close).
    for ts_val, group in df.group_by("ts_utc", maintain_order=True):
        ts_utc = ts_val[0]
        legs: list[OptionLeg] = []
        # Snapshot-level scalars are constant across all rows of one
        # ts_utc; pull from the first row.
        first = group.row(0, named=True)
        underlying_price = float(first.get("stock_price", 0.0))
        risk_free_rate = float(first.get("residual_rate", 0.0))

        for row in group.iter_rows(named=True):
            strike = float(row["strike"])
            expir = row["expir_date"]
            # Call leg.
            legs.append(OptionLeg(
                underlying=ticker,
                expiry=expir,
                strike=strike,
                side=OptionSide.CALL,
                bid=_safe_float(row.get("call_bid")),
                ask=_safe_float(row.get("call_ask")),
                iv=_safe_float(row.get("call_iv")),
                volume=_safe_int(row.get("call_volume")),
                open_interest=_safe_int(row.get("call_open_interest")),
                multiplier=multiplier,
            ))
            # Put leg.
            legs.append(OptionLeg(
                underlying=ticker,
                expiry=expir,
                strike=strike,
                side=OptionSide.PUT,
                bid=_safe_float(row.get("put_bid")),
                ask=_safe_float(row.get("put_ask")),
                iv=_safe_float(row.get("put_iv")),
                volume=_safe_int(row.get("put_volume")),
                open_interest=_safe_int(row.get("put_open_interest")),
                multiplier=multiplier,
            ))

        yield ChainSnapshot(
            underlying=ticker,
            ts_utc=ts_utc,
            underlying_price=underlying_price,
            risk_free_rate=risk_free_rate,
            legs=tuple(legs),
        )


def _safe_float(v) -> float:
    if v is None:
        return 0.0
    try:
        out = float(v)
    except (TypeError, ValueError):
        return 0.0
    if out != out:  # NaN guard — polars nulls round-trip as NaN floats sometimes
        return 0.0
    return out


def _safe_int(v) -> int:
    if v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0
