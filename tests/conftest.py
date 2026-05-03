"""Pytest fixtures for the feature pipeline tests.

We generate a synthetic ts/price/size tick CSV covering two short sessions
and point the registry loaders at tests/fixtures/registry/ so the tests
exercise the real loader/engine code paths against a controlled,
test-only DataSource (`synth_ticks`) and feature chain (`synth_*`).

The production registry under registries/ holds only parity-contract
sources (mes_5s_ohlcv, es_1s_ohlcv); deliberately keeping test fixtures
out of it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

from tradegy import config


_TESTS_REGISTRY_ROOT = Path(__file__).parent / "fixtures" / "registry"


def _generate_es_ticks(
    sessions: list[tuple[datetime, datetime]],
    *,
    seed: int = 42,
    base_price: float = 4500.0,
    tick_per_second: float = 2.0,
) -> pl.DataFrame:
    import random

    rng = random.Random(seed)
    ts: list[datetime] = []
    px: list[float] = []
    sz: list[int] = []
    price = base_price
    interval = timedelta(microseconds=int(1_000_000 / tick_per_second))
    for start, end in sessions:
        cur = start
        while cur < end:
            jitter_us = rng.randint(0, 1000)
            cur_ts = cur + timedelta(microseconds=jitter_us)
            shock = rng.gauss(0.0, 0.05)
            price = max(1.0, price + shock)
            size = max(1, int(abs(rng.gauss(5, 3))))
            ts.append(cur_ts)
            px.append(round(price, 2))
            sz.append(size)
            cur += interval
    return pl.DataFrame(
        {
            "ts": [t.isoformat() for t in ts],
            "price": px,
            "size": sz,
        }
    )


@pytest.fixture(scope="session")
def synthetic_csv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    sessions = [
        (
            datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc),
            datetime(2024, 1, 2, 15, 0, tzinfo=timezone.utc),
        ),
        (
            datetime(2024, 1, 3, 14, 30, tzinfo=timezone.utc),
            datetime(2024, 1, 3, 15, 0, tzinfo=timezone.utc),
        ),
    ]
    df = _generate_es_ticks(sessions, seed=42)
    out = tmp_path_factory.mktemp("csv") / "es_ticks_synth.csv"
    df.write_csv(out)
    return out


@pytest.fixture()
def workspace(tmp_path: Path) -> dict[str, Path]:
    raw = tmp_path / "raw"
    feats = tmp_path / "features"
    audits = tmp_path / "audits"
    for p in (raw, feats, audits):
        p.mkdir(parents=True)
    return {"raw": raw, "features": feats, "audits": audits, "root": tmp_path}


@pytest.fixture(autouse=True)
def _redirect_registry_to_test_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the registry loaders at tests/fixtures/registry/ for the test
    session so the production registries/ stay parity-contract-only.

    The synth_* registry entries here mirror the legacy es_* feature chain
    (tick → 1m bars → log returns → realized vol) but explicitly mark
    `synth_ticks` as a test-only source with no `live` block.
    """
    ds_dir = _TESTS_REGISTRY_ROOT / "data_sources"
    feat_dir = _TESTS_REGISTRY_ROOT / "features"
    monkeypatch.setattr(config, "data_sources_registry_dir", lambda: ds_dir)
    monkeypatch.setattr(config, "features_registry_dir", lambda: feat_dir)


# ── Real options-chain fixture ────────────────────────────────────
#
# Per the no-synthetic-data rule (memory: feedback_no_synthetic_data):
# every options-related behavior test exercises real ingested ORATS
# chain data, not hand-built ChainSnapshot instances. The fixture
# below resolves the on-disk path (gitignored, locally-pulled) and
# fails clearly with reproduction instructions if it isn't present.


@pytest.fixture(scope="session")
def real_spx_chain_snapshots():
    """Load every ingested SPX snapshot from
    `data/raw/source=spx_options_chain/`.

    Bypasses the autouse registry redirect by reading the parquet
    partitions directly via `iter_chain_snapshots(root=...)`. Fails
    clearly with the pull command if data isn't on disk.
    """
    from tradegy.options.chain_io import iter_chain_snapshots

    raw_root = config.repo_root() / "data" / "raw"
    if not (raw_root / "source=spx_options_chain").exists():
        pytest.fail(
            "Real ORATS SPX chain data is not on disk. "
            "Per the no-synthetic-data rule, options behavior tests "
            "exercise real chain data only. Pull it with:\n"
            "  python /Users/dan/code/data/download_spx_options_orats.py "
            "--start 2025-12-15 --end 2025-12-19 --confirm\n"
            "  uv run tradegy ingest "
            "/Users/dan/code/data/spx_options_orats/spx_options_orats.csv "
            "--source-id spx_options_chain"
        )
    snaps = list(iter_chain_snapshots(
        "spx_options_chain", ticker="SPX", root=raw_root,
    ))
    if not snaps:
        pytest.fail(
            "spx_options_chain partitions exist but contain no "
            "snapshots — re-pull a non-empty window."
        )
    return snaps
