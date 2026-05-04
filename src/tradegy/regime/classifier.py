"""Phase 3 distillation classifier.

Trains a deterministic gradient-boosted-tree classifier on numeric
session features (built by `session_inputs.build_snapshot`) → LLM-
emitted regime labels (Phase 2 output). The deterministic classifier
is what the strategy class consumes at backtest time, preserving the
walk-forward integrity the LLM cannot.

Train mode: walk-forward over rolling windows. For each fold:
  * Train: trailing 3yr of labeled sessions (relative to the fold's
    cut date).
  * Predict: next 6mo of sessions.
  * Emit predicted_label + confidence per session in the test fold.

Output: a feature-shaped parquet keyed by ts_utc=session-open-UTC
with columns (regime_label_predicted: int category, regime_confidence:
float). The classifier instance is also persisted so we can reproduce
predictions deterministically.

For now training+predict is a single offline command; the resulting
parquet gets wired up as a registered feature so the harness picks it
up via the standard feature-panel asof join.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import polars as pl

from tradegy.regime.session_inputs import (
    SessionPreOpenSnapshot,
    build_snapshot,
    _session_open_close_utc,
)
from tradegy.regime.session_labels import RegimeLabel, SessionLabel


_log = logging.getLogger(__name__)
REGIME_CLASSES: tuple[str, ...] = (
    "range", "trend_up", "trend_down", "news_driven", "uncertain"
)


# ── feature extraction ──────────────────────────────────────────────


@dataclass(frozen=True)
class TrainingExample:
    session_date: date
    feature_vector: dict[str, float]
    regime_label: str


_NUMERIC_FEATURE_NAMES: tuple[str, ...] = (
    "overnight_gap_pct",
    "prior_1d_return",
    "prior_2d_return",
    "prior_3d_return",
    "prior_4d_return",
    "prior_5d_return",
    "prior_5d_cumulative_return",
    "vix_close",
    "vix_pctile_252",
    "vix_5d_change",
    "n_high_importance_events_today",
    "n_medium_importance_events_today",
    "any_event_today",
    "day_of_week",
)


def snapshot_to_feature_vector(
    s: SessionPreOpenSnapshot,
) -> dict[str, float]:
    """Convert a SessionPreOpenSnapshot to a numeric feature vector
    suitable for training. Missing values get None which sklearn
    handles via missing-data-aware HistGradientBoostingClassifier."""
    fv: dict[str, float] = {}
    fv["overnight_gap_pct"] = (
        float(s.overnight_gap_pct) if s.overnight_gap_pct is not None
        else float("nan")
    )
    # Prior returns: pad with nan if fewer than 5 days available.
    rets = list(s.prior_5d_close_to_close_pct) + [float("nan")] * 5
    for i in range(5):
        fv[f"prior_{i + 1}d_return"] = float(rets[i])
    # Cumulative: sum over available days.
    cum = sum(r for r in s.prior_5d_close_to_close_pct if not _is_nan(r))
    fv["prior_5d_cumulative_return"] = float(cum)
    fv["vix_close"] = (
        float(s.vix_close_at_prior_close)
        if s.vix_close_at_prior_close is not None else float("nan")
    )
    fv["vix_pctile_252"] = (
        float(s.vix_pctile_252_at_prior_close)
        if s.vix_pctile_252_at_prior_close is not None else float("nan")
    )
    fv["vix_5d_change"] = (
        float(s.vix_5d_change_at_prior_close)
        if s.vix_5d_change_at_prior_close is not None else float("nan")
    )
    n_high = sum(
        1 for e in s.scheduled_events_today if e.importance == "high"
    )
    n_medium = sum(
        1 for e in s.scheduled_events_today if e.importance == "medium"
    )
    fv["n_high_importance_events_today"] = float(n_high)
    fv["n_medium_importance_events_today"] = float(n_medium)
    fv["any_event_today"] = float(1 if s.scheduled_events_today else 0)
    fv["day_of_week"] = float(s.session_date.weekday())
    return fv


def _is_nan(x: float) -> bool:
    return x != x  # NaN-only test


def feature_matrix(
    examples: list[TrainingExample],
) -> tuple[np.ndarray, np.ndarray]:
    """Build (X, y) arrays from a list of TrainingExample. y is integer-
    encoded class label per REGIME_CLASSES order."""
    X = np.full((len(examples), len(_NUMERIC_FEATURE_NAMES)), np.nan)
    for i, ex in enumerate(examples):
        for j, name in enumerate(_NUMERIC_FEATURE_NAMES):
            X[i, j] = ex.feature_vector[name]
    y = np.array(
        [REGIME_CLASSES.index(ex.regime_label) for ex in examples],
        dtype=np.int64,
    )
    return X, y


# ── data loading ────────────────────────────────────────────────────


def load_labeled_examples(
    *,
    instrument: str,
    labels_dir: Path,
    raw_root: Path | None = None,
    feature_root: Path | None = None,
) -> list[TrainingExample]:
    """Load all SessionLabels for `instrument` from disk and pair each
    with its computed feature vector."""
    inst_dir = labels_dir / instrument.upper()
    if not inst_dir.exists():
        return []
    examples: list[TrainingExample] = []
    for path in sorted(inst_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            label = SessionLabel.model_validate(data)
        except Exception as exc:
            _log.warning("skipping unreadable %s: %r", path, exc)
            continue
        try:
            snap = build_snapshot(
                session_date=label.session_date,
                instrument=instrument,
                raw_root=raw_root,
                feature_root=feature_root,
            )
        except FileNotFoundError as exc:
            _log.warning("no bars for %s: %r", label.session_date, exc)
            continue
        if snap.today_open is None:
            continue
        fv = snapshot_to_feature_vector(snap)
        examples.append(TrainingExample(
            session_date=label.session_date,
            feature_vector=fv,
            regime_label=label.regime_label,
        ))
    return examples


# ── training ────────────────────────────────────────────────────────


@dataclass
class WalkForwardFold:
    fold_index: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date


def walk_forward_folds(
    *,
    earliest: date,
    latest: date,
    train_window_days: int = 365 * 3,
    test_window_days: int = 30 * 6,
    holdout_days: int = 30 * 6,
) -> list[WalkForwardFold]:
    """Generate non-overlapping test windows tiled forward from
    `earliest`. Each fold trains on the trailing `train_window_days`
    BEFORE its test_start. Last `holdout_days` days are reserved (no
    training, no test) — used by Phase 5 holdout evaluation.
    """
    folds: list[WalkForwardFold] = []
    cursor = earliest + timedelta(days=train_window_days)
    holdout_cutoff = latest - timedelta(days=holdout_days)
    idx = 0
    while cursor <= holdout_cutoff:
        train_end = cursor
        train_start = train_end - timedelta(days=train_window_days)
        test_start = cursor
        test_end = min(cursor + timedelta(days=test_window_days), holdout_cutoff)
        folds.append(WalkForwardFold(
            fold_index=idx,
            train_start=train_start, train_end=train_end,
            test_start=test_start, test_end=test_end,
        ))
        cursor = test_end
        idx += 1
    return folds


@dataclass
class FoldResult:
    fold: WalkForwardFold
    n_train: int
    n_test: int
    macro_f1: float | None
    confusion: dict[str, dict[str, int]]
    feature_importances: dict[str, float]


def train_walk_forward(
    examples: list[TrainingExample],
    *,
    folds: list[WalkForwardFold],
) -> tuple[list[FoldResult], pl.DataFrame]:
    """Train one HistGradientBoostingClassifier per fold; emit
    per-session predicted_label + confidence for the test sessions of
    every fold concatenated.

    Returns (fold_results, predictions_df). predictions_df has columns:
      session_date, regime_label_predicted, regime_confidence
    """
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import f1_score

    by_date = {ex.session_date: ex for ex in examples}
    results: list[FoldResult] = []
    pred_rows: list[dict] = []

    for fold in folds:
        train_ex = [
            ex for ex in examples
            if fold.train_start <= ex.session_date < fold.train_end
        ]
        test_ex = [
            ex for ex in examples
            if fold.test_start <= ex.session_date < fold.test_end
        ]
        if len(train_ex) < 30 or len(test_ex) < 5:
            _log.info(
                "fold %d skipped: train=%d test=%d (need >=30 train, >=5 test)",
                fold.fold_index, len(train_ex), len(test_ex),
            )
            continue

        X_train, y_train = feature_matrix(train_ex)
        X_test, y_test = feature_matrix(test_ex)

        clf = HistGradientBoostingClassifier(
            max_iter=200,
            max_depth=6,
            learning_rate=0.05,
            random_state=42,
        )
        clf.fit(X_train, y_train)
        y_pred = clf.predict(X_test)
        y_proba = clf.predict_proba(X_test)
        # Per-prediction confidence = the probability of the predicted class.
        confidences = np.array(
            [y_proba[i, y_pred[i]] for i in range(len(y_pred))]
        )

        macro_f1 = float(
            f1_score(y_test, y_pred, average="macro", zero_division=0)
        )

        # Confusion matrix as dict-of-dicts.
        confusion: dict[str, dict[str, int]] = {
            t: {p: 0 for p in REGIME_CLASSES} for t in REGIME_CLASSES
        }
        for t, p in zip(y_test, y_pred):
            confusion[REGIME_CLASSES[int(t)]][REGIME_CLASSES[int(p)]] += 1

        # Feature importances are NOT directly exposed for HGBT in sklearn
        # < 1.4 but permutation_importance is available; fall back to
        # uniform if unavailable.
        try:
            from sklearn.inspection import permutation_importance
            r = permutation_importance(
                clf, X_train, y_train, n_repeats=3, random_state=42, n_jobs=1,
            )
            importances = {
                _NUMERIC_FEATURE_NAMES[i]: float(r.importances_mean[i])
                for i in range(len(_NUMERIC_FEATURE_NAMES))
            }
        except Exception:
            importances = {n: 0.0 for n in _NUMERIC_FEATURE_NAMES}

        results.append(FoldResult(
            fold=fold,
            n_train=len(train_ex),
            n_test=len(test_ex),
            macro_f1=macro_f1,
            confusion=confusion,
            feature_importances=importances,
        ))

        for i, ex in enumerate(test_ex):
            pred_rows.append({
                "session_date": ex.session_date,
                "regime_label_predicted": REGIME_CLASSES[int(y_pred[i])],
                "regime_confidence": float(confidences[i]),
                "fold_index": fold.fold_index,
            })

    if pred_rows:
        pred_df = pl.DataFrame(pred_rows).sort("session_date")
    else:
        pred_df = pl.DataFrame(
            schema={
                "session_date": pl.Date,
                "regime_label_predicted": pl.String,
                "regime_confidence": pl.Float64,
                "fold_index": pl.Int64,
            }
        )
    return results, pred_df


def predictions_to_feature_parquet(
    pred_df: pl.DataFrame,
    *,
    instrument: str,
    out_dir: Path,
    label_value_dir: Path,
    confidence_value_dir: Path,
) -> tuple[int, int]:
    """Materialize the classifier predictions as feature-shaped
    parquets that the harness can asof-join into the bar panel.

    Two outputs (one per derived feature):
      * `<inst>_regime_label_predicted` — one row per session at
        09:30 ET (DST-aware UTC). Value is the class index (0-4).
      * `<inst>_regime_confidence` — same cadence, value is the
        proba.

    Class-index encoding: see REGIME_CLASSES order.
    Returns (n_label_rows, n_conf_rows).
    """
    if pred_df.height == 0:
        return 0, 0

    # Build session-open UTC timestamps.
    rows = pred_df.to_dicts()
    label_rows: list[dict] = []
    conf_rows: list[dict] = []
    for r in rows:
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

    # Write per-date partitions. Match the existing feature parquet
    # convention: data/features/feature=<id>/version=v1/date=YYYY-MM-DD/data.parquet
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

    n_label = _write_partitions(label_df, label_value_dir)
    n_conf = _write_partitions(conf_df, confidence_value_dir)
    return n_label, n_conf
