"""Filesystem layout and shared constants."""
from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    return repo_root() / "data"


def raw_dir() -> Path:
    return data_dir() / "raw"


def raw_csv_dir() -> Path:
    return data_dir() / "raw_csv"


def features_dir() -> Path:
    return data_dir() / "features"


def audits_dir() -> Path:
    return data_dir() / "audits"


def evidence_dir() -> Path:
    return data_dir() / "evidence"


def registries_dir() -> Path:
    return repo_root() / "registries"


def data_sources_registry_dir() -> Path:
    return registries_dir() / "data_sources"


def features_registry_dir() -> Path:
    return registries_dir() / "features"


def strategy_specs_dir() -> Path:
    """Where strategy spec YAMLs live. Separate from the registries/
    folder because specs reference registry classes, not the other way
    around."""
    return repo_root() / "strategies"
