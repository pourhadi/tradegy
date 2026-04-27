"""Pytest fixtures for the feature pipeline tests.

We generate a synthetic ES-shaped tick CSV (timestamp, price, size) covering
two trading sessions. Synthetic, but the schema and timezone semantics match
what real ES tick CSVs look like, so the same code paths run end-to-end. A
real CSV can be dropped into data/raw_csv/ for manual end-to-end testing.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import polars as pl


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
