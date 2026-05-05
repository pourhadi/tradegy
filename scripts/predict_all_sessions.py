#!/usr/bin/env python3
"""Train ONE classifier on all labeled examples and predict for every
session in the bar history. Materializes the predictions as feature
parquets.

NOT walk-forward-honest — the classifier sees every label (training
window = full label set). Use this only for exploratory MVP-level
testing of strategy specs that consume the regime features. For
walk-forward-discipline use `tradegy train-regime-classifier`.

Plan ref: discretion call after Anthropic credits ran out at 855/1789
labels — gives us complete-coverage predictions for backtest, with
the explicit caveat that a positive backtest result does NOT confirm
the regime classifier's edge (training-data leakage). A negative
result IS informative (the gate doesn't help even with perfect-info
training).
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Iterable

import numpy as np
import polars as pl
from sklearn.ensemble import HistGradientBoostingClassifier

from tradegy import config
from tradegy.regime.classifier import (
    REGIME_CLASSES,
    feature_matrix,
    load_labeled_examples,
    snapshot_to_feature_vector,
)
from tradegy.regime.session_inputs import (
    _read_bars,
    _read_econ_events,
    _read_feature_value,
    _session_open_close_utc,
    aggregate_to_session_daily,
    build_snapshot,
)


def main() -> int:
    instrument = sys.argv[1] if len(sys.argv) > 1 else "MES"

    print(f"[1/6] Loading labeled examples for {instrument}...")
    labels_dir = config.data_dir() / "session_labels"
    examples = load_labeled_examples(
        instrument=instrument, labels_dir=labels_dir,
    )
    print(f"      {len(examples)} examples")
    if not examples:
        print("ERROR: no labeled examples; run label-sessions first.")
        return 1

    print(f"[2/6] Building feature matrix...")
    X_train, y_train = feature_matrix(examples)
    print(f"      X_train shape: {X_train.shape}")
    from collections import Counter
    print(f"      class distribution:")
    for cls_idx, cnt in Counter(y_train.tolist()).most_common():
        print(f"        {REGIME_CLASSES[cls_idx]}: {cnt}")

    print(f"[3/6] Training HistGradientBoostingClassifier on full labeled set...")
    clf = HistGradientBoostingClassifier(
        max_iter=200,
        max_depth=6,
        learning_rate=0.05,
        random_state=42,
    )
    clf.fit(X_train, y_train)
    print(f"      train accuracy: {clf.score(X_train, y_train):.3f}")

    print(f"[4/6] Pre-loading shared data for prediction...")
    bars = _read_bars(instrument)
    session_daily = aggregate_to_session_daily(bars)
    events = _read_econ_events()
    vix_close_df = _read_feature_value("vix_daily_close")
    vix_pctile_df = _read_feature_value("vix_daily_pctile_252")
    vix_5d_df = _read_feature_value("vix_daily_5d_change")

    print(f"[5/6] Building snapshots + predicting for every session...")
    all_dates = session_daily["session_date"].to_list()
    print(f"      {len(all_dates):,} sessions to predict")

    pred_rows: list[dict] = []
    skipped = 0
    for i, sd in enumerate(all_dates):
        if isinstance(sd, str):
            sd = date.fromisoformat(sd)
        snap = build_snapshot(
            session_date=sd, instrument=instrument,
            bars=bars, session_daily=session_daily,
            events=events, vix_close_df=vix_close_df,
            vix_pctile_df=vix_pctile_df, vix_5d_df=vix_5d_df,
        )
        if snap.today_open is None:
            skipped += 1
            continue
        fv = snapshot_to_feature_vector(snap)
        x_pred = np.array(
            [[fv[name] for name in [
                "overnight_gap_pct",
                "prior_1d_return", "prior_2d_return", "prior_3d_return",
                "prior_4d_return", "prior_5d_return",
                "prior_5d_cumulative_return",
                "vix_close", "vix_pctile_252", "vix_5d_change",
                "n_high_importance_events_today",
                "n_medium_importance_events_today",
                "any_event_today",
                "day_of_week",
            ]]],
            dtype=np.float64,
        )
        pred = int(clf.predict(x_pred)[0])
        proba = clf.predict_proba(x_pred)[0]
        pred_rows.append({
            "session_date": sd,
            "regime_label_predicted": REGIME_CLASSES[pred],
            "regime_confidence": float(proba[pred]),
            "fold_index": -1,  # sentinel: extrapolated, not walk-forward
        })
    print(f"      predicted: {len(pred_rows):,}; skipped (no bars): {skipped:,}")

    if not pred_rows:
        print("ERROR: no predictions produced.")
        return 1

    print(f"[6/6] Materializing feature parquets...")
    pred_df = pl.DataFrame(pred_rows).sort("session_date")

    # Build per-feature parquets at session-open UTC.
    label_dir = (
        config.features_dir()
        / f"feature={instrument.lower()}_regime_label_predicted"
        / "version=v1"
    )
    conf_dir = (
        config.features_dir()
        / f"feature={instrument.lower()}_regime_confidence"
        / "version=v1"
    )

    label_rows: list[dict] = []
    conf_rows: list[dict] = []
    for r in pred_df.to_dicts():
        sd = r["session_date"]
        if isinstance(sd, str):
            sd = date.fromisoformat(sd)
        open_utc, _ = _session_open_close_utc(sd)
        cls_idx = REGIME_CLASSES.index(r["regime_label_predicted"])
        label_rows.append({"ts_utc": open_utc, "value": float(cls_idx)})
        conf_rows.append({
            "ts_utc": open_utc, "value": float(r["regime_confidence"]),
        })

    label_df = pl.DataFrame(label_rows).sort("ts_utc")
    conf_df = pl.DataFrame(conf_rows).sort("ts_utc")

    def _write_partitions(df: pl.DataFrame, root: Path) -> int:
        n = 0
        for part in df.partition_by("ts_utc", maintain_order=True):
            ts = part.row(0, named=True)["ts_utc"]
            d = ts.date().isoformat()
            target = root / f"date={d}"
            target.mkdir(parents=True, exist_ok=True)
            part.write_parquet(target / "data.parquet")
            n += 1
        return n

    n_label = _write_partitions(label_df, label_dir)
    n_conf = _write_partitions(conf_df, conf_dir)
    print(f"      label partitions written: {n_label}")
    print(f"      confidence partitions written: {n_conf}")

    # Distribution
    print(f"\nPredicted regime distribution across full session set:")
    label_dist = pred_df.group_by("regime_label_predicted").agg(
        pl.len().alias("count")
    ).sort("count", descending=True)
    for row in label_dist.to_dicts():
        pct = 100.0 * row["count"] / pred_df.height
        print(f"  {row['regime_label_predicted']}: {row['count']} ({pct:.1f}%)")

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
