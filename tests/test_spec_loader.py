"""Strategy spec loader + validator (Phase 2D).

Covers: round-trip loading, registry-reference validation (good and
bad), parameter envelope membership, schema version check, hard-max
stop guard.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tradegy.specs import load_spec, validate_spec
from tradegy.specs.loader import SpecValidationError
from tradegy.specs.schema import StrategySpec


def _minimal_spec_dict() -> dict:
    return {
        "metadata": {
            "id": "mes_momentum_test",
            "version": "0.1.0",
            "schema_version": "1.0",
            "name": "MES Momentum Test",
            "status": "draft",
            "created_date": "2026-04-28",
            "last_modified_date": "2026-04-28",
            "author": "dan",
        },
        "market_scope": {
            "instrument": "MES",
            "session": "globex",
        },
        "entry": {
            "strategy_class": "momentum_breakout",
            "parameters": {
                "return_feature_id": "mes_5m_log_returns",
                "entry_threshold": 0.001,
                "max_attempts_per_session": 1,
            },
            "direction": "long",
            "entry_order_type": "market",
        },
        "sizing": {
            "method": "fixed_contracts",
            "parameters": {"contracts": 1},
        },
        "stops": {
            "initial_stop": {"method": "fixed_ticks", "stop_ticks": 20, "tick_size": 0.25},
            "hard_max_distance_ticks": 100,
            "time_stop": {"enabled": True, "max_holding_bars": 30},
        },
        "exits": {
            "invalidation_conditions": [
                {
                    "condition": "feature_threshold",
                    "parameters": {
                        "feature_id": "mes_realized_vol_30m",
                        "operator": "gt",
                        "threshold": 0.50,
                    },
                    "action": "exit_market",
                }
            ],
        },
    }


def test_minimal_spec_validates(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(_minimal_spec_dict()))
    spec = load_spec(spec_path)
    assert isinstance(spec, StrategySpec)
    assert spec.metadata.id == "mes_momentum_test"
    assert spec.entry.strategy_class == "momentum_breakout"


def test_unknown_strategy_class_rejected(tmp_path: Path) -> None:
    raw = _minimal_spec_dict()
    raw["entry"]["strategy_class"] = "nonexistent_class"
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(raw))
    with pytest.raises(SpecValidationError, match="strategy_class"):
        load_spec(spec_path)


def test_unknown_sizing_method_rejected(tmp_path: Path) -> None:
    raw = _minimal_spec_dict()
    raw["sizing"]["method"] = "nonexistent_sizing"
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(raw))
    with pytest.raises(SpecValidationError, match="sizing.method"):
        load_spec(spec_path)


def test_unknown_stop_method_rejected(tmp_path: Path) -> None:
    raw = _minimal_spec_dict()
    raw["stops"]["initial_stop"]["method"] = "nonexistent_stop"
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(raw))
    with pytest.raises(SpecValidationError, match="stops.initial_stop.method"):
        load_spec(spec_path)


def test_unknown_invalidation_condition_rejected(tmp_path: Path) -> None:
    raw = _minimal_spec_dict()
    raw["exits"]["invalidation_conditions"][0]["condition"] = "nonexistent_cond"
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(raw))
    with pytest.raises(SpecValidationError, match="invalidation_conditions"):
        load_spec(spec_path)


def test_strategy_class_parameter_validation_fires(tmp_path: Path) -> None:
    raw = _minimal_spec_dict()
    raw["entry"]["parameters"]["entry_threshold"] = 1.0  # > 0.05 max
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(raw))
    with pytest.raises(SpecValidationError, match="entry.parameters"):
        load_spec(spec_path)


def test_unsupported_schema_version_rejected(tmp_path: Path) -> None:
    raw = _minimal_spec_dict()
    raw["metadata"]["schema_version"] = "99.0"
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(raw))
    with pytest.raises(SpecValidationError, match="schema_version"):
        load_spec(spec_path)


def test_parameter_envelope_violation(tmp_path: Path) -> None:
    raw = _minimal_spec_dict()
    raw["entry"]["parameters"]["entry_threshold"] = 0.005
    raw["parameter_envelope"] = {
        "entry_threshold": {"tested_min": 0.0005, "tested_max": 0.002},
    }
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(raw))
    with pytest.raises(SpecValidationError, match="outside tested envelope"):
        load_spec(spec_path)


def test_parameter_envelope_within_range_passes(tmp_path: Path) -> None:
    raw = _minimal_spec_dict()
    raw["entry"]["parameters"]["entry_threshold"] = 0.001
    raw["parameter_envelope"] = {
        "entry_threshold": {"tested_min": 0.0005, "tested_max": 0.002},
    }
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(raw))
    spec = load_spec(spec_path)
    assert spec.parameter_envelope is not None


def test_stop_ticks_above_hard_max_rejected(tmp_path: Path) -> None:
    raw = _minimal_spec_dict()
    raw["stops"]["hard_max_distance_ticks"] = 10
    raw["stops"]["initial_stop"]["stop_ticks"] = 50
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(raw))
    with pytest.raises(SpecValidationError, match="hard_max_distance_ticks"):
        load_spec(spec_path)
