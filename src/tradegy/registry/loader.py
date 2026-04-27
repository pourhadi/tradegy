"""YAML <-> pydantic loaders for the data-source and feature registries.

Registry entries live as YAML files under registries/{data_sources,features}/
so they're human-reviewable and version-controllable, matching the
"deliberate friction" tone of 02_feature_pipeline.md:484.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from tradegy import config
from tradegy.types import DataSource, Feature


def load_data_source(source_id: str, *, registry_root: Path | None = None) -> DataSource:
    base = registry_root or config.data_sources_registry_dir()
    path = base / f"{source_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"data source registry entry not found: {path}")
    with path.open() as f:
        raw = yaml.safe_load(f)
    return DataSource.model_validate(raw)


def load_feature(feature_id: str, *, registry_root: Path | None = None) -> Feature:
    base = registry_root or config.features_registry_dir()
    path = base / f"{feature_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"feature registry entry not found: {path}")
    with path.open() as f:
        raw = yaml.safe_load(f)
    return Feature.model_validate(raw)


def list_features(*, registry_root: Path | None = None) -> list[Feature]:
    base = registry_root or config.features_registry_dir()
    if not base.exists():
        return []
    out: list[Feature] = []
    for path in sorted(base.glob("*.yaml")):
        with path.open() as f:
            raw = yaml.safe_load(f)
        out.append(Feature.model_validate(raw))
    return out


__all__ = ["load_data_source", "load_feature", "list_features"]
