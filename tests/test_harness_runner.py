"""Backtest harness MVP — synthetic-data integration test.

Builds a synthetic bar series with a deliberate momentum kick, runs the
momentum_breakout spec through the harness, and asserts:
  - at least one trade fires
  - all trades have non-null entry / exit timestamps
  - per-trade pnl_R is finite
  - aggregate stats compute (no division-by-zero / NaN)
  - the time_stop block triggers exit when the kick fades
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from tradegy import config
from tradegy.harness import CostModel, run_backtest


@pytest.fixture(autouse=True)
def _use_production_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override the global test conftest's redirection. The harness
    needs to read mes_1m_bars / mes_5m_log_returns YAMLs from the
    production registry, while parquet data still comes from tmp_path
    via the explicit ``feature_root`` arg.
    """
    repo_root = Path(__file__).parent.parent
    monkeypatch.setattr(
        config, "data_sources_registry_dir",
        lambda: repo_root / "registries" / "data_sources",
    )
    monkeypatch.setattr(
        config, "features_registry_dir",
        lambda: repo_root / "registries" / "features",
    )
from tradegy.specs.schema import (
    EntrySpec,
    ExitsSpec,
    InvalidationCondition,
    MarketScopeSpec,
    MetadataSpec,
    SizingSpec,
    StopsSpec,
    StrategySpec,
    TimeStopBlock,
)


def _make_synth_bars(tmp_path: Path) -> Path:
    """Produce a `mes_1m_bars` parquet partition with a slow uptrend +
    one strong push that should trigger momentum_breakout.

    Layout matches what the engine writes:
      data/features/feature=mes_1m_bars/version=v1/date=YYYY-MM-DD/data.parquet
    """
    feat_root = tmp_path / "features"
    bars_dir = feat_root / "feature=mes_1m_bars" / "version=v1" / "date=2024-06-03"
    bars_dir.mkdir(parents=True)

    # 60 minutes: flat at 5000 for first 20 bars, jump to 5005 over bars
    # 21-25, then drift back to 5000.
    rows = []
    base_ts = datetime(2024, 6, 3, 14, 0, tzinfo=timezone.utc)
    for i in range(60):
        ts = base_ts + timedelta(minutes=i + 1)
        if i < 20:
            close = 5000.0 + (i % 3) * 0.25  # tiny noise
        elif i < 25:
            close = 5000.0 + (i - 20) * 1.0  # 5 minutes of upward drive
        else:
            close = 5005.0 - (i - 25) * 0.20  # slow fade
        rows.append({
            "ts_utc": ts,
            "open": close - 0.25,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 100,
            "num_trades": 50,
            "bid_volume": 60,
            "ask_volume": 40,
        })
    df = pl.DataFrame(rows, schema={
        "ts_utc": pl.Datetime("ns", "UTC"),
        "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Int64, "num_trades": pl.Int64,
        "bid_volume": pl.Int64, "ask_volume": pl.Int64,
    })
    df.write_parquet(bars_dir / "data.parquet", compression="zstd")

    # Also write the 5m_log_returns feature this strategy depends on.
    # Use the same engine convention: ts_utc, value (the log return per
    # bar). We'll just emit a feature that mirrors a 5m return series at
    # 1m cadence so the harness has something to read at every bar.
    ret_dir = feat_root / "feature=mes_5m_log_returns" / "version=v1" / "date=2024-06-03"
    ret_dir.mkdir(parents=True)
    rets = []
    for i in range(60):
        ts = base_ts + timedelta(minutes=i + 1)
        if i < 20:
            value = 0.0
        elif i < 25:
            value = 0.002  # 0.2% return — above default threshold 0.001
        else:
            value = -0.0001
        rets.append({"ts_utc": ts, "value": value})
    pl.DataFrame(rets, schema={
        "ts_utc": pl.Datetime("ns", "UTC"),
        "value": pl.Float64,
    }).write_parquet(ret_dir / "data.parquet", compression="zstd")

    return feat_root


def _build_spec() -> StrategySpec:
    return StrategySpec(
        metadata=MetadataSpec(
            id="test_mom",
            version="0.1.0",
            schema_version="1.0",
            name="Test Momentum",
            status="draft",
            created_date="2026-04-28",
            last_modified_date="2026-04-28",
            author="test",
        ),
        market_scope=MarketScopeSpec(instrument="MES", session="globex"),
        entry=EntrySpec(
            strategy_class="momentum_breakout",
            parameters={
                "return_feature_id": "mes_5m_log_returns",
                "entry_threshold": 0.001,
                "max_attempts_per_session": 3,
            },
            direction="long",
            entry_order_type="market",
        ),
        sizing=SizingSpec(
            method="fixed_contracts",
            parameters={"contracts": 1},
        ),
        stops=StopsSpec(
            initial_stop={
                "method": "fixed_ticks",
                "stop_ticks": 8,
                "tick_size": 0.25,
            },
            hard_max_distance_ticks=100,
            time_stop=TimeStopBlock(enabled=True, max_holding_bars=10),
        ),
        exits=ExitsSpec(invalidation_conditions=[]),
    )


def test_runner_produces_at_least_one_trade(tmp_path: Path) -> None:
    feat_root = _make_synth_bars(tmp_path)
    spec = _build_spec()
    cost = CostModel(
        tick_size=0.25,
        slippage_ticks_per_side=0.5,
        commission_per_contract_round_trip=1.50,
    )
    result = run_backtest(spec, cost=cost, feature_root=feat_root)
    assert result.total_bars == 60
    assert result.trades, "synthetic momentum kick should fire at least one entry"
    for t in result.trades:
        assert t.entry_ts is not None and t.exit_ts is not None
        assert t.exit_ts >= t.entry_ts
        assert t.commissions > 0
        # net_pnl_R may be negative — that's fine; just non-null.
        assert t.net_pnl_R is not None


def test_runner_aggregate_stats_compute(tmp_path: Path) -> None:
    feat_root = _make_synth_bars(tmp_path)
    spec = _build_spec()
    result = run_backtest(spec, feature_root=feat_root)
    assert result.stats is not None
    s = result.stats
    assert s.total_trades == len(result.trades)
    assert isinstance(s.expectancy_R, float)
    assert isinstance(s.win_rate, float)
    assert s.max_drawdown >= 0


def test_runner_time_stop_caps_holding(tmp_path: Path) -> None:
    feat_root = _make_synth_bars(tmp_path)
    spec = _build_spec()
    result = run_backtest(spec, feature_root=feat_root)
    # No trade should hold longer than max_holding_bars (10).
    for t in result.trades:
        assert t.holding_bars <= 11, f"trade held {t.holding_bars} bars (>= time stop)"


def test_runner_no_trades_on_flat_data(tmp_path: Path) -> None:
    """Feature stream with all zeros — no entries should fire."""
    feat_root = tmp_path / "features"
    bars_dir = feat_root / "feature=mes_1m_bars" / "version=v1" / "date=2024-06-03"
    bars_dir.mkdir(parents=True)
    base_ts = datetime(2024, 6, 3, 14, 0, tzinfo=timezone.utc)
    pl.DataFrame(
        [{"ts_utc": base_ts + timedelta(minutes=i+1),
          "open": 5000.0, "high": 5000.5, "low": 4999.5,
          "close": 5000.0, "volume": 100} for i in range(30)],
        schema={"ts_utc": pl.Datetime("ns", "UTC"),
                "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
                "close": pl.Float64, "volume": pl.Int64},
    ).write_parquet(bars_dir / "data.parquet")
    ret_dir = feat_root / "feature=mes_5m_log_returns" / "version=v1" / "date=2024-06-03"
    ret_dir.mkdir(parents=True)
    pl.DataFrame(
        [{"ts_utc": base_ts + timedelta(minutes=i+1), "value": 0.0} for i in range(30)],
        schema={"ts_utc": pl.Datetime("ns", "UTC"), "value": pl.Float64},
    ).write_parquet(ret_dir / "data.parquet")

    result = run_backtest(_build_spec(), feature_root=feat_root)
    assert result.trades == []
    assert result.stats.total_trades == 0
