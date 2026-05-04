"""Unit tests for the regime classifier subsystem.

Covers:
  - SessionLabel / SessionLabelBatch Pydantic schemas (validation,
    rejection of out-of-range fields).
  - AnthropicSessionLabeler with a mocked Anthropic client (verifies
    prompt construction + JSON parsing + cost recording without
    touching the real API).
  - snapshot_to_feature_vector: deterministic conversion shape.
  - walk_forward_folds: tiling correctness.
  - feature_matrix: round-trip from examples to numpy arrays.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from tradegy.regime.classifier import (
    REGIME_CLASSES,
    TrainingExample,
    feature_matrix,
    snapshot_to_feature_vector,
    walk_forward_folds,
)
from tradegy.regime.session_inputs import (
    ScheduledEventInput,
    SessionPreOpenSnapshot,
    format_snapshot_for_llm,
)
from tradegy.regime.session_labeling import (
    AnthropicSessionLabeler,
    _build_user_message,
    _extract_json_blob,
    _parse_batch,
)
from tradegy.regime.session_labels import (
    LabeledNewsEvent,
    SessionLabel,
    SessionLabelBatch,
)


# ── schema tests ────────────────────────────────────────────────────


def test_session_label_validates_basic():
    lbl = SessionLabel(
        session_date=date(2024, 9, 18),
        instrument="MES",
        regime_label="news_driven",
        regime_confidence=0.93,
        regime_signals=["fomc_today"],
        news_events=[],
        reasoning="FOMC SEP day, expect vol expansion.",
    )
    assert lbl.regime_label == "news_driven"


def test_session_label_rejects_out_of_range_confidence():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SessionLabel(
            session_date=date(2024, 9, 18),
            instrument="MES",
            regime_label="range",
            regime_confidence=1.5,  # > 1
            reasoning="x",
        )


def test_session_label_rejects_unknown_regime():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SessionLabel(
            session_date=date(2024, 9, 18),
            instrument="MES",
            regime_label="bullish",  # not in Literal
            regime_confidence=0.7,
            reasoning="x",
        )


def test_labeled_news_event_halt_window_clamped():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        LabeledNewsEvent(
            event_ts_utc=datetime(2024, 9, 18, 18, tzinfo=timezone.utc),
            event_type="fomc_statement",
            market_impact="regime_changing",
            halt_recommended_window_minutes=600,  # > 480 max
        )


# ── snapshot formatting + JSON-blob extraction ──────────────────────


def _sample_snapshot(d: date) -> SessionPreOpenSnapshot:
    return SessionPreOpenSnapshot(
        session_date=d,
        instrument="MES",
        overnight_gap_pct=0.0027,
        prior_close=5440.50,
        today_open=5455.00,
        prior_5d_close_to_close_pct=[0.0021, -0.0013, 0.0005, 0.0008, -0.0010],
        vix_close_at_prior_close=16.45,
        vix_pctile_252_at_prior_close=0.32,
        vix_5d_change_at_prior_close=-0.85,
        scheduled_events_today=[],
    )


def test_format_snapshot_includes_critical_fields():
    s = _sample_snapshot(date(2024, 8, 13))
    out = format_snapshot_for_llm(s)
    assert "2024-08-13" in out
    assert "MES" in out
    assert "+0.27%" in out  # overnight gap
    assert "16.45" in out  # VIX close
    assert "0.32" in out   # VIX pctile


def test_format_snapshot_with_events():
    s = SessionPreOpenSnapshot(
        session_date=date(2024, 9, 18),
        instrument="MES",
        overnight_gap_pct=0.0,
        prior_close=5707.25,
        today_open=5707.25,
        prior_5d_close_to_close_pct=[],
        vix_close_at_prior_close=17.61,
        vix_pctile_252_at_prior_close=0.83,
        vix_5d_change_at_prior_close=-1.47,
        scheduled_events_today=[
            ScheduledEventInput(
                ts_utc=datetime(2024, 9, 18, 18, tzinfo=timezone.utc),
                event_type="fomc_sep",
                importance="high",
                headline="FOMC SEP release",
            ),
        ],
    )
    out = format_snapshot_for_llm(s)
    assert "fomc_sep" in out
    assert "[high]" in out


def test_extract_json_blob_finds_fenced_code():
    text = (
        "Some preamble.\n"
        '```json\n{"labels": []}\n```\n'
        "Trailing prose."
    )
    blob = _extract_json_blob(text)
    assert blob == '{"labels": []}'


def test_extract_json_blob_picks_largest_fenced():
    text = (
        '```json\n{"a": 1}\n```\n'
        '```json\n{"labels": [{"x": "long"}], "extra": "value"}\n```'
    )
    blob = _extract_json_blob(text)
    assert "labels" in blob


def test_parse_batch_valid_round_trip():
    batch = SessionLabelBatch(labels=[
        SessionLabel(
            session_date=date(2024, 9, 18),
            instrument="MES",
            regime_label="news_driven",
            regime_confidence=0.93,
            reasoning="x",
        ),
    ])
    json_text = batch.model_dump_json()
    parsed = _parse_batch(f"```json\n{json_text}\n```")
    assert parsed.labels[0].regime_label == "news_driven"


# ── prompt assembly ─────────────────────────────────────────────────


def test_user_message_lists_all_snapshots():
    snaps = [
        _sample_snapshot(date(2024, 8, 13)),
        _sample_snapshot(date(2024, 8, 14)),
    ]
    msg = _build_user_message(snaps)
    assert "Label the following 2 session" in msg
    assert "2024-08-13" in msg
    assert "2024-08-14" in msg


def test_user_message_empty_snapshots_raises():
    with pytest.raises(ValueError, match="at least one snapshot"):
        _build_user_message([])


# ── labeler with mock client ────────────────────────────────────────


@dataclass
class _FakeBlock:
    type: str = "text"
    text: str = ""


@dataclass
class _FakeUsage:
    input_tokens: int = 1000
    output_tokens: int = 200
    cache_creation_input_tokens: int = 5000
    cache_read_input_tokens: int = 0


class _FakeMessages:
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.last_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        return type(
            "FakeResponse", (),
            {
                "content": [_FakeBlock(text=self.response_text)],
                "usage": _FakeUsage(),
            },
        )()


class _FakeClient:
    def __init__(self, response_text: str):
        self.messages = _FakeMessages(response_text)


def test_labeler_round_trip_with_mock_client():
    snaps = [_sample_snapshot(date(2024, 8, 13))]
    response = SessionLabelBatch(labels=[
        SessionLabel(
            session_date=date(2024, 8, 13),
            instrument="MES",
            regime_label="range",
            regime_confidence=0.78,
            regime_signals=["low_vix_pctile"],
            news_events=[],
            reasoning="Quiet day.",
        ),
    ]).model_dump_json()

    client = _FakeClient(f"```json\n{response}\n```")
    labeler = AnthropicSessionLabeler(client=client)
    out = labeler.generate(snaps)

    assert len(out) == 1
    assert out[0].regime_label == "range"
    assert out[0].regime_confidence == 0.78
    # Verify cost was recorded.
    assert labeler.last_cost is not None
    assert labeler.last_cost.input_tokens == 1000


def test_labeler_count_mismatch_raises():
    snaps = [
        _sample_snapshot(date(2024, 8, 13)),
        _sample_snapshot(date(2024, 8, 14)),
    ]
    response = SessionLabelBatch(labels=[
        SessionLabel(
            session_date=date(2024, 8, 13),
            instrument="MES",
            regime_label="range",
            regime_confidence=0.78,
            reasoning="x",
        ),
    ]).model_dump_json()
    client = _FakeClient(f"```json\n{response}\n```")
    labeler = AnthropicSessionLabeler(client=client)
    with pytest.raises(ValueError, match="order or count mismatch"):
        labeler.generate(snaps)


def test_labeler_cache_breakpoint_in_system_blocks():
    """Verify the schema block carries cache_control — required for
    economic prompt-caching of the multi-batch run."""
    snaps = [_sample_snapshot(date(2024, 8, 13))]
    response = SessionLabelBatch(labels=[
        SessionLabel(
            session_date=date(2024, 8, 13),
            instrument="MES",
            regime_label="range",
            regime_confidence=0.7,
            reasoning="x",
        ),
    ]).model_dump_json()
    client = _FakeClient(f"```json\n{response}\n```")
    labeler = AnthropicSessionLabeler(client=client)
    labeler.generate(snaps)

    sys_blocks = client.messages.last_kwargs["system"]
    cached = [b for b in sys_blocks if "cache_control" in b]
    assert len(cached) >= 1, "at least one system block should carry cache_control"


# ── classifier feature extraction ───────────────────────────────────


def test_snapshot_to_feature_vector_shape():
    s = _sample_snapshot(date(2024, 8, 13))
    fv = snapshot_to_feature_vector(s)
    # All declared feature names present.
    assert set(fv.keys()) == {
        "overnight_gap_pct",
        "prior_1d_return", "prior_2d_return", "prior_3d_return",
        "prior_4d_return", "prior_5d_return",
        "prior_5d_cumulative_return",
        "vix_close", "vix_pctile_252", "vix_5d_change",
        "n_high_importance_events_today",
        "n_medium_importance_events_today",
        "any_event_today",
        "day_of_week",
    }


def test_snapshot_to_feature_vector_no_events():
    s = _sample_snapshot(date(2024, 8, 13))
    fv = snapshot_to_feature_vector(s)
    assert fv["any_event_today"] == 0.0
    assert fv["n_high_importance_events_today"] == 0.0


def test_snapshot_to_feature_vector_with_high_event():
    s = SessionPreOpenSnapshot(
        session_date=date(2024, 9, 18),
        instrument="MES",
        overnight_gap_pct=0.0,
        prior_close=100.0,
        today_open=100.0,
        prior_5d_close_to_close_pct=[],
        vix_close_at_prior_close=17.5,
        vix_pctile_252_at_prior_close=0.5,
        vix_5d_change_at_prior_close=0.0,
        scheduled_events_today=[
            ScheduledEventInput(
                ts_utc=datetime(2024, 9, 18, 18, tzinfo=timezone.utc),
                event_type="fomc_statement",
                importance="high",
                headline="x",
            ),
        ],
    )
    fv = snapshot_to_feature_vector(s)
    assert fv["any_event_today"] == 1.0
    assert fv["n_high_importance_events_today"] == 1.0


def test_snapshot_to_feature_vector_pads_missing_returns():
    s = SessionPreOpenSnapshot(
        session_date=date(2019, 5, 10),
        instrument="MES",
        overnight_gap_pct=0.001,
        prior_close=2900.0,
        today_open=2903.0,
        prior_5d_close_to_close_pct=[0.001, -0.002],  # only 2 days
        vix_close_at_prior_close=15.0,
        vix_pctile_252_at_prior_close=0.4,
        vix_5d_change_at_prior_close=0.0,
        scheduled_events_today=[],
    )
    fv = snapshot_to_feature_vector(s)
    assert fv["prior_1d_return"] == 0.001
    assert fv["prior_2d_return"] == -0.002
    # Days 3-5 should be NaN (padded).
    import math
    assert math.isnan(fv["prior_3d_return"])
    assert math.isnan(fv["prior_4d_return"])
    assert math.isnan(fv["prior_5d_return"])


# ── feature_matrix + walk_forward_folds ─────────────────────────────


def test_feature_matrix_shape():
    snap = _sample_snapshot(date(2024, 8, 13))
    fv = snapshot_to_feature_vector(snap)
    examples = [
        TrainingExample(
            session_date=date(2024, 8, 13),
            feature_vector=fv,
            regime_label="range",
        ),
        TrainingExample(
            session_date=date(2024, 8, 14),
            feature_vector=fv,
            regime_label="trend_up",
        ),
    ]
    X, y = feature_matrix(examples)
    assert X.shape == (2, 14)
    assert y.tolist() == [REGIME_CLASSES.index("range"),
                          REGIME_CLASSES.index("trend_up")]


def test_walk_forward_folds_basic_layout():
    folds = walk_forward_folds(
        earliest=date(2019, 1, 1),
        latest=date(2025, 1, 1),
        train_window_days=365 * 3,
        test_window_days=180,
        holdout_days=180,
    )
    assert len(folds) > 0
    # First fold should have train_start ~3yr before earliest+3yr.
    f0 = folds[0]
    assert (f0.train_end - f0.train_start).days == 365 * 3
    # No fold should overlap the holdout.
    holdout_cutoff = date(2025, 1, 1) - __import__("datetime").timedelta(days=180)
    for f in folds:
        assert f.test_end <= holdout_cutoff


def test_walk_forward_folds_no_folds_if_insufficient_history():
    folds = walk_forward_folds(
        earliest=date(2024, 1, 1),
        latest=date(2024, 6, 1),
        train_window_days=365 * 3,
        test_window_days=180,
        holdout_days=180,
    )
    # Need 3yr + test + holdout = 3.5yr worth of dates; we only have 5mo.
    assert folds == []
