"""Walk-forward validation harness — window split + per-window math + gate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from tradegy import config
from tradegy.harness import (
    CostModel,
    WalkForwardConfig,
    run_walk_forward,
    split_windows,
)
from tradegy.harness.walk_forward import _evaluate_gate, WalkForwardSummary
from tradegy.specs.schema import (
    EntrySpec,
    ExitsSpec,
    MarketScopeSpec,
    MetadataSpec,
    SizingSpec,
    StopsSpec,
    StrategySpec,
    TimeStopBlock,
)


@pytest.fixture(autouse=True)
def _use_production_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = Path(__file__).parent.parent
    monkeypatch.setattr(
        config, "data_sources_registry_dir",
        lambda: repo_root / "registries" / "data_sources",
    )
    monkeypatch.setattr(
        config, "features_registry_dir",
        lambda: repo_root / "registries" / "features",
    )


# ---------- pure helpers (no I/O) ----------


def test_split_windows_produces_rolling_train_test_pairs() -> None:
    cov_start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    cov_end = datetime(2025, 1, 1, tzinfo=timezone.utc)
    cfg = WalkForwardConfig(train_years=3.0, test_years=1.0, roll_years=1.0)
    wins = split_windows(cov_start, cov_end, cfg)

    # Train 3y + test 1y = 4y span; rolling 1y over 5y total → 2 windows.
    assert len(wins) == 2
    assert wins[0].train_start == cov_start
    assert wins[0].test_end <= cov_end
    # Second window train_start is +1y from the first.
    assert (wins[1].train_start - wins[0].train_start).days >= 360


def test_split_windows_returns_empty_when_span_too_short() -> None:
    cov_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    cov_end = datetime(2024, 6, 1, tzinfo=timezone.utc)  # 5 months
    cfg = WalkForwardConfig(train_years=3.0, test_years=1.0)
    assert split_windows(cov_start, cov_end, cfg) == []


def test_walk_forward_config_rejects_nonpositive_durations() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        WalkForwardConfig(train_years=0)
    with pytest.raises(ValueError, match="must be positive"):
        WalkForwardConfig(test_years=-1.0)


def test_gate_fails_when_in_sample_not_positive() -> None:
    s = WalkForwardSummary(
        spec_id="x", spec_version="1.0",
        config=WalkForwardConfig(),
        coverage_start=datetime.now(tz=timezone.utc),
        coverage_end=datetime.now(tz=timezone.utc),
        windows=[object()],  # type: ignore[list-item]
    )
    s.avg_in_sample_sharpe = -0.1
    s.avg_oos_sharpe = -0.05
    passed, reason = _evaluate_gate(s)
    assert passed is False
    assert "no in-sample edge" in reason


def test_gate_passes_when_oos_within_half_of_in_sample() -> None:
    s = WalkForwardSummary(
        spec_id="x", spec_version="1.0",
        config=WalkForwardConfig(),
        coverage_start=datetime.now(tz=timezone.utc),
        coverage_end=datetime.now(tz=timezone.utc),
        windows=[object()],  # type: ignore[list-item]
    )
    s.avg_in_sample_sharpe = 0.8
    s.avg_oos_sharpe = 0.5  # ratio 0.625 ≥ 0.5
    assert _evaluate_gate(s) == (True, "")


def test_gate_fails_when_oos_collapses() -> None:
    s = WalkForwardSummary(
        spec_id="x", spec_version="1.0",
        config=WalkForwardConfig(),
        coverage_start=datetime.now(tz=timezone.utc),
        coverage_end=datetime.now(tz=timezone.utc),
        windows=[object()],  # type: ignore[list-item]
    )
    s.avg_in_sample_sharpe = 0.8
    s.avg_oos_sharpe = 0.2  # ratio 0.25 < 0.5
    passed, reason = _evaluate_gate(s)
    assert passed is False
    assert "< 50% of in-sample" in reason


# ---------- end-to-end on synthetic data ----------


def _build_synth_data(tmp_path: Path) -> Path:
    """3-year synthetic mes_1m_bars + mes_5m_log_returns at 1-day resolution.

    We don't need 1m granularity to exercise the walk-forward window split
    — coarse daily bars are enough and keep the test fast.
    """
    feat_root = tmp_path / "features"
    bars_dir = feat_root / "feature=mes_1m_bars" / "version=v1"
    rets_dir = feat_root / "feature=mes_5m_log_returns" / "version=v1"

    rows: list[dict] = []
    rets: list[dict] = []
    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    # 3 years × 250 trading days × 1 bar/day = 750 bars. Place each at
    # 14:30 UTC on a weekday so they fall inside CMES sessions.
    day = base
    n = 0
    while n < 750:
        if day.weekday() < 5:  # Mon-Fri
            ts = day.replace(hour=14, minute=30)
            close = 5000.0 + (n % 50) * 0.5
            rows.append({
                "ts_utc": ts,
                "open": close - 0.25, "high": close + 0.5, "low": close - 0.5,
                "close": close,
                "volume": 100, "num_trades": 50,
                "bid_volume": 60, "ask_volume": 40,
            })
            # Keep returns positive enough to fire the strategy.
            rets.append({"ts_utc": ts, "value": 0.005})
            n += 1
        day = day + timedelta(days=1)

    bars_df = pl.DataFrame(rows, schema={
        "ts_utc": pl.Datetime("ns", "UTC"),
        "open": pl.Float64, "high": pl.Float64, "low": pl.Float64,
        "close": pl.Float64,
        "volume": pl.Int64, "num_trades": pl.Int64,
        "bid_volume": pl.Int64, "ask_volume": pl.Int64,
    })
    rets_df = pl.DataFrame(rets, schema={
        "ts_utc": pl.Datetime("ns", "UTC"), "value": pl.Float64,
    })

    # Single partition is fine (reads pattern is glob over date=*).
    bd = bars_dir / "date=2020-01-01"
    bd.mkdir(parents=True)
    bars_df.write_parquet(bd / "data.parquet")
    rd = rets_dir / "date=2020-01-01"
    rd.mkdir(parents=True)
    rets_df.write_parquet(rd / "data.parquet")
    return feat_root


def _build_spec() -> StrategySpec:
    return StrategySpec(
        metadata=MetadataSpec(
            id="wf_test", version="0.1.0", schema_version="1.0",
            name="WF Test", status="draft",
            created_date="2026-04-28", last_modified_date="2026-04-28",
            author="test",
        ),
        market_scope=MarketScopeSpec(instrument="MES", session="globex"),
        entry=EntrySpec(
            strategy_class="momentum_breakout",
            parameters={
                "return_feature_id": "mes_5m_log_returns",
                "entry_threshold": 0.001,
                "max_attempts_per_session": 1,
            },
            direction="long",
            entry_order_type="market",
        ),
        sizing=SizingSpec(method="fixed_contracts", parameters={"contracts": 1}),
        stops=StopsSpec(
            initial_stop={"method": "fixed_ticks", "stop_ticks": 8, "tick_size": 0.25},
            hard_max_distance_ticks=100,
            time_stop=TimeStopBlock(enabled=True, max_holding_bars=10),
        ),
        exits=ExitsSpec(invalidation_conditions=[]),
    )


def test_walk_forward_runs_end_to_end_and_populates_summary(tmp_path: Path) -> None:
    feat_root = _build_synth_data(tmp_path)
    spec = _build_spec()
    cfg = WalkForwardConfig(train_years=1.0, test_years=0.5, roll_years=0.5)
    summary = run_walk_forward(
        spec,
        coverage_start=datetime(2020, 1, 1, tzinfo=timezone.utc),
        coverage_end=datetime(2023, 1, 1, tzinfo=timezone.utc),
        config=cfg,
        feature_root=feat_root,
    )
    assert summary.windows, "expected at least one window"
    assert all(
        w.in_sample is not None and w.out_of_sample is not None
        for w in summary.windows
    )
    # Aggregates were computed.
    assert isinstance(summary.avg_in_sample_sharpe, float)
    assert isinstance(summary.avg_oos_sharpe, float)
