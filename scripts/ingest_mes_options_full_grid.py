#!/usr/bin/env python3
"""Ingest the full MES options CSV grid into mes_options_chain.

Discovers every (definition, ohlcv-1m) CSV pair under
`/Users/dan/code/data/mes_options_*` and unions them all into a
single source via `ingest_databento_options_grid`.  Quarterlies
+ all 20 X-prefix daily parents.

Usage:
    uv run python scripts/ingest_mes_options_full_grid.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from tradegy.ingest.databento_options import ingest_databento_options_grid
from tradegy.registry.loader import load_data_source

DATA_ROOT = Path("/Users/dan/code/data")


def discover_pairs() -> list[tuple[Path, Path]]:
    """Return (definition_csv, ohlcv_csv) pairs across all parents."""
    pairs: list[tuple[Path, Path]] = []

    # Quarterlies: mes_options_definition + mes_options_ohlcv_1m
    quart_def_dir = DATA_ROOT / "mes_options_definition"
    quart_bars_dir = DATA_ROOT / "mes_options_ohlcv_1m"
    if quart_def_dir.exists() and quart_bars_dir.exists():
        for def_csv in sorted(quart_def_dir.glob("*.csv")):
            window = def_csv.stem.replace("mes_options_definition_", "")
            bars_csv = quart_bars_dir / f"mes_options_ohlcv_1m_{window}.csv"
            if bars_csv.exists():
                pairs.append((def_csv, bars_csv))

    # Dailies: mes_options_daily_x<W><D>_definition + _ohlcv_1m
    for week in (1, 2, 3, 4, 5):
        for dow in ("a", "b", "c", "d"):
            slug = f"x{week}{dow}"
            def_dir = DATA_ROOT / f"mes_options_daily_{slug}_definition"
            bars_dir = DATA_ROOT / f"mes_options_daily_{slug}_ohlcv_1m"
            if not (def_dir.exists() and bars_dir.exists()):
                continue
            for def_csv in sorted(def_dir.glob("*.csv")):
                window = def_csv.stem.replace(f"{slug}_definition_", "")
                bars_csv = bars_dir / f"{slug}_ohlcv_1m_{window}.csv"
                if bars_csv.exists():
                    pairs.append((def_csv, bars_csv))

    return pairs


def main() -> int:
    pairs = discover_pairs()
    if not pairs:
        print("ERROR: no CSV pairs found under /Users/dan/code/data/mes_options_*/",
              file=sys.stderr)
        return 1

    print(f"Discovered {len(pairs)} CSV pairs:")
    for def_csv, bars_csv in pairs:
        print(f"  {def_csv.name}  ⨯  {bars_csv.name}")
    print()

    source = load_data_source("mes_options_chain")
    print(f"Ingesting into source={source.id} v{source.version}")
    print()

    result = ingest_databento_options_grid(pairs, source)
    print(f"\n=== Final IngestResult ===")
    print(f"  source_id:           {result.source_id}")
    print(f"  batch_id:            {result.batch_id}")
    print(f"  raw_path:            {result.raw_path}")
    print(f"  rows_in (last pair): {result.rows_in:,}")
    print(f"  rows_out (cumulative): {result.rows_out:,}")
    print(f"  combo bars filtered: {result.duplicates_dropped:,}")
    print(f"  coverage:            {result.coverage_start} → {result.coverage_end}")
    print(f"  partitions written:  {len(result.partitions_written)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
