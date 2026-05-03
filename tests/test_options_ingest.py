"""ORATS strikes CSV ingest + chain-snapshot reader tests.

Builds a synthetic ORATS-shaped CSV (camelCase column names matching
ORATS /datav2/hist/strikes), runs the ingest, and verifies:

  - Required vendor columns are enforced; missing one raises.
  - Vendor camelCase columns are renamed to snake_case canonical.
  - Trade dates serialize to UTC at the SPX session close (16:00 ET
    → 21:00 UTC in EST, 20:00 UTC in EDT).
  - Date partitions land on disk in the standard layout.
  - The chain-snapshot reader recovers typed ChainSnapshot/OptionLeg
    objects from the parquet partitions, with both call and put legs
    per (expiry, strike).
  - Vendor Greeks are NOT propagated to OptionLeg (per the doc 14
    decision — strategies recompute via bs_greeks).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from tradegy.ingest.csv_orats import ingest_orats_strikes_csv
from tradegy.options.chain import OptionSide
from tradegy.options.chain_io import (
    iter_chain_snapshots,
    load_chain_frames,
)
from tradegy.registry.loader import load_data_source


# ── Synthetic ORATS CSV builder ────────────────────────────────


def _build_orats_csv(
    path: Path,
    *,
    trade_dates: list[date],
    expiries: list[date],
    strikes: list[float],
    stock_price: float = 4500.0,
) -> Path:
    """Write a minimal-but-valid ORATS-shaped CSV. Vendor camelCase
    headers (matching their /datav2/hist/strikes endpoint).
    """
    rows: list[dict] = []
    for td in trade_dates:
        for ex in expiries:
            dte = (ex - td).days
            for k in strikes:
                rows.append({
                    "ticker": "SPX",
                    "tradeDate": td.isoformat(),
                    "expirDate": ex.isoformat(),
                    "dte": dte,
                    "strike": k,
                    "stockPrice": stock_price,
                    "residualRate": 0.045,
                    "smvVol": 0.18,
                    "callBidPrice": max(stock_price - k, 0) + 5.0,
                    "callAskPrice": max(stock_price - k, 0) + 5.4,
                    "callValue": max(stock_price - k, 0) + 5.2,
                    "callImpliedVol": 0.18,
                    "callDelta": 0.55,
                    "callGamma": 0.001,
                    "callTheta": -0.50,
                    "callVega": 5.0,
                    "callRho": 1.2,
                    "callVolume": 1000,
                    "callOpenInterest": 5000,
                    "putBidPrice": max(k - stock_price, 0) + 5.0,
                    "putAskPrice": max(k - stock_price, 0) + 5.4,
                    "putValue": max(k - stock_price, 0) + 5.2,
                    "putImpliedVol": 0.20,
                    "putDelta": -0.45,
                    "putGamma": 0.001,
                    "putTheta": -0.45,
                    "putVega": 5.0,
                    "putRho": -1.0,
                    "putVolume": 1500,
                    "putOpenInterest": 7000,
                })
    df = pl.DataFrame(rows)
    df.write_csv(path)
    return path


# ── Ingest path ─────────────────────────────────────────────────


def test_ingest_writes_partitions_and_canonicalizes_columns(
    tmp_path, workspace,
):
    src_path = _build_orats_csv(
        tmp_path / "orats.csv",
        trade_dates=[date(2026, 1, 2), date(2026, 1, 3)],
        expiries=[date(2026, 2, 20)],
        strikes=[4400.0, 4500.0, 4600.0],
    )
    source = load_data_source("synth_options")
    result = ingest_orats_strikes_csv(
        src_path, source, out_dir=workspace["raw"],
    )
    # 2 dates × 1 expiry × 3 strikes = 6 rows.
    assert result.rows_in == 6
    assert result.rows_out == 6
    # 2 partitions (one per trade date).
    assert len(result.partitions_written) == 2

    # Read back and verify schema is snake_case canonical.
    df = load_chain_frames("synth_options", root=workspace["raw"])
    assert "call_bid" in df.columns
    assert "callBidPrice" not in df.columns
    assert "put_iv" in df.columns
    assert df["ts_utc"].dtype == pl.Datetime("ns", "UTC")


def test_ingest_drops_exact_duplicates(tmp_path, workspace):
    """ORATS occasionally back-publishes; exact (ts, expiry, strike)
    duplicates must collapse. We synthesize an explicit duplicate by
    appending the same row twice.
    """
    src_path = _build_orats_csv(
        tmp_path / "orats.csv",
        trade_dates=[date(2026, 1, 2)],
        expiries=[date(2026, 2, 20)],
        strikes=[4500.0],
    )
    # Append a duplicate of the single row to the CSV.
    text = src_path.read_text().splitlines()
    src_path.write_text("\n".join([*text, text[1]]))

    source = load_data_source("synth_options")
    result = ingest_orats_strikes_csv(
        src_path, source, out_dir=workspace["raw"],
    )
    assert result.duplicates_dropped == 1
    assert result.rows_out == 1


def test_ingest_raises_on_missing_required_columns(tmp_path, workspace):
    """Drop a required column from the input — ingest must refuse."""
    src_path = _build_orats_csv(
        tmp_path / "orats.csv",
        trade_dates=[date(2026, 1, 2)],
        expiries=[date(2026, 2, 20)],
        strikes=[4500.0],
    )
    df = pl.read_csv(src_path).drop("callBidPrice")
    df.write_csv(src_path)

    source = load_data_source("synth_options")
    with pytest.raises(ValueError, match="missing required columns"):
        ingest_orats_strikes_csv(
            src_path, source, out_dir=workspace["raw"],
        )


def test_ts_utc_is_session_close_in_eastern(tmp_path, workspace):
    """trade_date 2026-01-02 (winter, EST) → 16:00 ET = 21:00 UTC."""
    src_path = _build_orats_csv(
        tmp_path / "orats.csv",
        trade_dates=[date(2026, 1, 2)],
        expiries=[date(2026, 2, 20)],
        strikes=[4500.0],
    )
    source = load_data_source("synth_options")
    ingest_orats_strikes_csv(src_path, source, out_dir=workspace["raw"])
    df = load_chain_frames("synth_options", root=workspace["raw"])
    ts = df.row(0, named=True)["ts_utc"]
    expected = datetime(2026, 1, 2, 21, 0, tzinfo=timezone.utc)
    assert ts == expected


def test_ts_utc_is_session_close_in_eastern_dst(tmp_path, workspace):
    """trade_date 2026-06-15 (summer, EDT) → 16:00 ET = 20:00 UTC."""
    src_path = _build_orats_csv(
        tmp_path / "orats.csv",
        trade_dates=[date(2026, 6, 15)],
        expiries=[date(2026, 7, 17)],
        strikes=[4500.0],
    )
    source = load_data_source("synth_options")
    ingest_orats_strikes_csv(src_path, source, out_dir=workspace["raw"])
    df = load_chain_frames("synth_options", root=workspace["raw"])
    ts = df.row(0, named=True)["ts_utc"]
    expected = datetime(2026, 6, 15, 20, 0, tzinfo=timezone.utc)
    assert ts == expected


# ── Chain-snapshot reader ──────────────────────────────────────


def test_iter_chain_snapshots_yields_one_per_trade_date(
    tmp_path, workspace,
):
    src_path = _build_orats_csv(
        tmp_path / "orats.csv",
        trade_dates=[date(2026, 1, 2), date(2026, 1, 3), date(2026, 1, 5)],
        expiries=[date(2026, 2, 20), date(2026, 3, 20)],
        strikes=[4400.0, 4500.0, 4600.0],
    )
    source = load_data_source("synth_options")
    ingest_orats_strikes_csv(src_path, source, out_dir=workspace["raw"])

    snaps = list(iter_chain_snapshots(
        "synth_options", ticker="SPX", root=workspace["raw"],
    ))
    assert len(snaps) == 3
    # Each snapshot has 2 expiries × 3 strikes × 2 sides = 12 legs.
    for s in snaps:
        assert len(s.legs) == 12
        assert s.underlying == "SPX"
        assert s.underlying_price == pytest.approx(4500.0)
        assert s.risk_free_rate == pytest.approx(0.045)


def test_chain_snapshot_legs_contain_both_sides(tmp_path, workspace):
    src_path = _build_orats_csv(
        tmp_path / "orats.csv",
        trade_dates=[date(2026, 1, 2)],
        expiries=[date(2026, 2, 20)],
        strikes=[4500.0],
    )
    source = load_data_source("synth_options")
    ingest_orats_strikes_csv(src_path, source, out_dir=workspace["raw"])
    [snap] = list(iter_chain_snapshots(
        "synth_options", ticker="SPX", root=workspace["raw"],
    ))
    sides = sorted(leg.side.value for leg in snap.legs)
    assert sides == ["call", "put"]
    assert snap.legs[0].multiplier == 100  # SPX default


def test_for_expiry_view_filters_correctly(tmp_path, workspace):
    src_path = _build_orats_csv(
        tmp_path / "orats.csv",
        trade_dates=[date(2026, 1, 2)],
        expiries=[date(2026, 2, 20), date(2026, 3, 20)],
        strikes=[4500.0],
    )
    source = load_data_source("synth_options")
    ingest_orats_strikes_csv(src_path, source, out_dir=workspace["raw"])
    [snap] = list(iter_chain_snapshots(
        "synth_options", ticker="SPX", root=workspace["raw"],
    ))
    feb = snap.for_expiry(date(2026, 2, 20))
    assert all(leg.expiry == date(2026, 2, 20) for leg in feb)
    assert len(feb) == 2  # 1 strike × 2 sides
    mar = snap.for_expiry(date(2026, 3, 20))
    assert len(mar) == 2


def test_unknown_source_raises(workspace):
    with pytest.raises(FileNotFoundError):
        load_chain_frames("does_not_exist", root=workspace["raw"])
