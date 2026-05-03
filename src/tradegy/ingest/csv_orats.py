"""Stage 1 — ORATS Pro options-chain CSV ingest.

ORATS Pro publishes end-of-day options chains via the
`/datav2/hist/strikes` endpoint. Each row covers one (tradeDate,
expirDate, strike) and contains both the call leg and the put leg
side-by-side, plus underlying close, ORATS-implied risk-free rate
("residualRate"), the smoothed market vol surface output
("smvVol"), and ORATS-published Greeks for both legs.

Per `trading_platform_docs/14_options_volatility_selling.md` the
harness IGNORES ORATS-published Greeks and recomputes via
`tradegy.options.greeks.bs_greeks` from per-leg IV. We still
ingest the vendor Greeks columns because they are useful for
cross-check audits (a >5% disagreement in delta between vendor
and our recompute is a valuable signal that something in the IV
extraction or the dividend-yield assumption is off).

Output schema (canonicalized column names with snake_case):

    ts_utc        Datetime[ns, UTC]   tradeDate at session close
                                       (16:00 ET = 20:00/21:00 UTC,
                                       depending on DST)
    ticker        String              "SPX"
    trade_date    Date                snapshot date
    expir_date    Date                expiration date for this row
    dte           Int64               days to expiration
    strike        Float64             contract strike
    stock_price   Float64             underlying close
    residual_rate Float64             ORATS implied risk-free rate
    smv_vol       Float64             ORATS smoothed-surface vol
    call_*        per-call columns    bid/ask/value/iv/delta/gamma/
                                       theta/vega/rho/volume/oi
    put_*         per-put columns     same shape

ORATS' raw CSV columns are camelCase; we snake_case on ingest so
downstream consumers don't have to remember vendor casing.

The session-close UTC mapping is +20h (16:00 ET) Mar→Nov DST and
+21h (16:00 ET, EST) Nov→Mar. We apply the mapping per-row using
zoneinfo so daylight transitions are handled correctly.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from tradegy.ingest._common import (
    IngestResult,
    compute_batch_id,
    now_utc_iso,
    sha256_file,
    source_root,
    write_date_partitions,
    write_receipt,
)
from tradegy.types import DataSource


# Vendor → canonical column name mapping. Verified against the
# actual ORATS /datav2/hist/strikes JSON response 2026-05-03 — the
# schema differs from the public docs in two important ways:
#
# 1. There is no callDelta/putDelta. ORATS publishes a single
#    `delta`/`gamma`/`theta`/`vega`/`rho`/`phi` per row which is
#    the CALL-side Greek (put delta = call_delta - 1 for European
#    options). We rename to vendor_call_delta etc. to make the
#    "this is the call leg's Greek" explicit and to avoid any
#    illusion that the same value applies to both legs.
# 2. There is no callImpliedVol/putImpliedVol. ORATS publishes
#    callBidIv/callMidIv/callAskIv (and the put equivalents) so
#    the per-side IV from quotes is preserved. We use callMidIv/
#    putMidIv as the canonical "leg IV" (mapped to call_iv /
#    put_iv) since strategies need a single IV per leg; the bid/
#    ask IVs are kept for spread-quality analysis.
#
# Anything not in this map is dropped on ingest (we want a known
# schema, not a passthrough).
_ORATS_COLUMN_MAP: dict[str, str] = {
    "ticker": "ticker",
    "tradeDate": "trade_date",
    "expirDate": "expir_date",
    "dte": "dte",
    "strike": "strike",
    "stockPrice": "stock_price",
    "spotPrice": "spot_price",
    "residualRate": "residual_rate",
    "smvVol": "smv_vol",
    "extSmvVol": "ext_smv_vol",
    "quoteDate": "quote_date",
    "expiryTod": "expiry_tod",       # "am" or "pm" — matters for SOQ
    # Call leg
    "callBidPrice": "call_bid",
    "callAskPrice": "call_ask",
    "callValue": "call_value",
    "callBidIv": "call_bid_iv",
    "callMidIv": "call_iv",          # canonical per-leg IV (mid)
    "callAskIv": "call_ask_iv",
    "callBidSize": "call_bid_size",
    "callAskSize": "call_ask_size",
    "callVolume": "call_volume",
    "callOpenInterest": "call_open_interest",
    "extCallValue": "ext_call_value",
    # Put leg
    "putBidPrice": "put_bid",
    "putAskPrice": "put_ask",
    "putValue": "put_value",
    "putBidIv": "put_bid_iv",
    "putMidIv": "put_iv",            # canonical per-leg IV (mid)
    "putAskIv": "put_ask_iv",
    "putBidSize": "put_bid_size",
    "putAskSize": "put_ask_size",
    "putVolume": "put_volume",
    "putOpenInterest": "put_open_interest",
    "extPutValue": "ext_put_value",
    # Vendor Greeks (call-side per ORATS — we prefix to make this
    # explicit and to discourage downstream consumption). Strategies
    # recompute via tradegy.options.greeks.bs_greeks; vendor Greeks
    # are kept for cross-check audits only.
    "delta": "vendor_call_delta",
    "gamma": "vendor_gamma",
    "theta": "vendor_call_theta",
    "vega": "vendor_vega",
    "rho": "vendor_call_rho",
    "phi": "vendor_phi",
    "driftlessTheta": "vendor_driftless_theta",
}

# Hard requirements — if any of these are missing the input is not
# usable and we refuse to ingest. Everything else is nice-to-have.
_REQUIRED_VENDOR_COLS: frozenset[str] = frozenset({
    "tradeDate",
    "expirDate",
    "strike",
    "stockPrice",
    "callBidPrice",
    "callAskPrice",
    "callMidIv",
    "putBidPrice",
    "putAskPrice",
    "putMidIv",
})

# US/Eastern is the SPX cash-options session zone. End-of-day chain
# snapshots are stamped at 16:00 ET (regular cash close); SPX
# options trade until 16:15 but ORATS' "tradeDate" snapshot uses
# 16:00 as the canonical close-of-business.
_SESSION_CLOSE_LOCAL = time(16, 0)
_SESSION_TZ = ZoneInfo("America/New_York")


def ingest_orats_strikes_csv(
    csv_path: Path,
    source: DataSource,
    *,
    out_dir: Path | None = None,
) -> IngestResult:
    """Ingest an ORATS /datav2/hist/strikes CSV into canonical
    parquet partitions, one partition per trade_date.

    The source's `IngestSpec.format` must be `orats_strikes_csv`.
    Output rows are sorted by (ts_utc, expir_date, strike). Vendor
    columns outside `_ORATS_COLUMN_MAP` are dropped silently;
    columns inside `_REQUIRED_VENDOR_COLS` raise on absence.
    """
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)
    if source.ingest is None or source.ingest.format != "orats_strikes_csv":
        raise ValueError(
            f"source {source.id!r} is not declared as orats_strikes_csv "
            "(ingest.format mismatch)"
        )

    out_root = source_root(source.id, out_dir=out_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    # ORATS volume / OI / size columns are mostly integers but
    # occasionally carry fractional values (corporate actions or
    # vendor adjustments). Coerce all numeric columns to Float64
    # up-front so polars schema inference doesn't trip on the
    # rare fractional row partway through a 4 GB file. The chain
    # reader downstream casts back to int via _safe_int when it
    # produces OptionLeg objects. Discovered 2026-05-03 on the
    # first XSP ingest — SPX never tripped this because SPX has
    # cleaner volume/OI data.
    _NUMERIC_FLOAT64 = {
        "dte": pl.Float64, "strike": pl.Float64, "stockPrice": pl.Float64,
        "spotPrice": pl.Float64, "residualRate": pl.Float64,
        "smvVol": pl.Float64, "extSmvVol": pl.Float64,
        "callBidPrice": pl.Float64, "callValue": pl.Float64,
        "callAskPrice": pl.Float64, "callBidIv": pl.Float64,
        "callMidIv": pl.Float64, "callAskIv": pl.Float64,
        "callVolume": pl.Float64, "callOpenInterest": pl.Float64,
        "callBidSize": pl.Float64, "callAskSize": pl.Float64,
        "extCallValue": pl.Float64,
        "putBidPrice": pl.Float64, "putValue": pl.Float64,
        "putAskPrice": pl.Float64, "putBidIv": pl.Float64,
        "putMidIv": pl.Float64, "putAskIv": pl.Float64,
        "putVolume": pl.Float64, "putOpenInterest": pl.Float64,
        "putBidSize": pl.Float64, "putAskSize": pl.Float64,
        "extPutValue": pl.Float64,
        "delta": pl.Float64, "gamma": pl.Float64, "theta": pl.Float64,
        "vega": pl.Float64, "rho": pl.Float64, "phi": pl.Float64,
        "driftlessTheta": pl.Float64,
    }
    raw = pl.read_csv(
        csv_path, infer_schema_length=10_000,
        schema_overrides=_NUMERIC_FLOAT64,
    )
    rows_in = raw.height
    missing = _REQUIRED_VENDOR_COLS - set(raw.columns)
    if missing:
        raise ValueError(
            f"orats_strikes_csv: input missing required columns "
            f"{sorted(missing)}"
        )

    # Drop columns we don't carry forward; rename the rest to snake_case.
    keep_vendor = [c for c in raw.columns if c in _ORATS_COLUMN_MAP]
    canon = raw.select(keep_vendor).rename(
        {v_col: _ORATS_COLUMN_MAP[v_col] for v_col in keep_vendor}
    )

    # Parse trade/expir dates. ORATS publishes YYYY-MM-DD strings.
    canon = canon.with_columns([
        pl.col("trade_date").str.to_date(format="%Y-%m-%d", strict=True),
        pl.col("expir_date").str.to_date(format="%Y-%m-%d", strict=True),
    ])

    # Compose ts_utc. Prefer the per-row `quote_date` ORATS publishes
    # (e.g. "2025-12-15T20:46:00Z" — the actual snapshot moment) so
    # backtests reflect when the chain was *actually* observable.
    # When that column is absent (older vintages, partial schema)
    # fall back to trade_date @ 16:00 America/New_York → UTC.
    if "quote_date" in canon.columns:
        canon = canon.with_columns(
            pl.col("quote_date")
            .str.to_datetime(time_unit="ns", time_zone="UTC", strict=True)
            .alias("ts_utc")
        )
    else:
        def _to_utc(d) -> datetime:
            local_dt = datetime.combine(d, _SESSION_CLOSE_LOCAL, _SESSION_TZ)
            return local_dt.astimezone(timezone.utc)

        ts_utc_values = [
            _to_utc(d) for d in canon.get_column("trade_date").to_list()
        ]
        canon = canon.with_columns(
            pl.Series("ts_utc", ts_utc_values).cast(pl.Datetime("ns", "UTC"))
        )

    # Drop exact (ts_utc, expir_date, strike) duplicates. ORATS
    # normally emits one row per key but back-published vintages can
    # overlap.
    pre_dedup = canon.height
    canon = canon.unique(
        subset=["ts_utc", "expir_date", "strike"], keep="last", maintain_order=True,
    )
    duplicates_dropped = pre_dedup - canon.height

    # Reorder for deterministic on-disk output.
    canon = canon.sort(["ts_utc", "expir_date", "strike"])

    rows_out = canon.height
    if rows_out == 0:
        raise ValueError(
            f"orats ingest produced zero rows from {csv_path}; check "
            "that the input contains at least one tradeDate row"
        )

    coverage_start = canon.select(pl.col("ts_utc").min()).item()
    coverage_end = canon.select(pl.col("ts_utc").max()).item()

    partitions_written = write_date_partitions(canon, out_root)

    batch_id = compute_batch_id(csv_path, source.id, source.version)
    write_receipt(
        out_root,
        batch_id,
        {
            "source_id": source.id,
            "source_version": source.version,
            "batch_id": batch_id,
            "format": "orats_strikes_csv",
            "csv_path": str(csv_path),
            "csv_sha256": sha256_file(csv_path),
            "ingested_at": now_utc_iso(),
            "rows_in": rows_in,
            "rows_out": rows_out,
            "duplicates_dropped": duplicates_dropped,
            "coverage_start": coverage_start.isoformat(),
            "coverage_end": coverage_end.isoformat(),
            "partitions": [str(p) for p in partitions_written],
            "vendor_columns_dropped": sorted(
                set(raw.columns) - set(_ORATS_COLUMN_MAP)
            ),
        },
    )

    return IngestResult(
        source_id=source.id,
        batch_id=batch_id,
        raw_path=out_root,
        rows_in=rows_in,
        rows_out=rows_out,
        duplicates_dropped=duplicates_dropped,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        partitions_written=partitions_written,
    )
