"""Strategy class abstract base + registration.

Per 03_strategy_class_registry.md:22-58, a strategy class is a
deterministic state machine that consumes Bar + FeatureSnapshot + Fill
events and emits Orders. Same code path runs in backtest and in live
(live/historical parity at the strategy layer, mirroring the parity
contract at the data layer).

Required properties (verified by tests, not by the framework):
- Determinism: (params, bar stream, feature stream, fill stream) →
  bit-exact identical actions.
- Statelessness at the class level: all session state lives in the State
  object; the class itself holds no session state.
- No LLM / network access: classes consume features and produce orders.
- No feature creation: classes consume features from the registry; new
  features go through the feature pipeline admission process.

Registration mirrors the transform and live-adapter patterns:

    @register_strategy_class("momentum_breakout")
    class MomentumBreakout(StrategyClass):
        id = "momentum_breakout"
        version = "v1"
        ...

The class registry is the firewall the docs call out: novel strategy
generation in YAML is impossible because every spec's `entry.strategy_class`
must resolve to a name in this registry.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from typing import Any

from tradegy.strategies.types import (
    Bar,
    ExitReason,
    FeatureSnapshot,
    Fill,
    Order,
    State,
)


class StrategyClass(ABC):
    """Abstract base for every registered strategy class.

    Subclasses MUST set:
        id              — registry name; matches register_strategy_class
        version         — semver string
        parameter_schema— dict[str, dict] declaring each parameter's
                          type/min/max/default. Used for spec validation.
        feature_dependencies — {"required": [...], "optional": [...]}
                               feature ids the class can reference.
    """

    id: str = "<unset>"
    version: str = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {}
    feature_dependencies: dict[str, list[str]] = {"required": [], "optional": []}

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        """Return a list of validation error messages (empty list = ok).

        Default implementation enforces type, min, max from
        `parameter_schema`. Subclasses override for cross-parameter
        invariants.
        """
        errors: list[str] = []
        for name, spec in self.parameter_schema.items():
            if name not in params:
                if "default" not in spec:
                    errors.append(f"missing required parameter '{name}'")
                continue
            value = params[name]
            expected_type = spec.get("type")
            if expected_type == "integer" and not isinstance(value, int):
                errors.append(f"{name}: expected integer, got {type(value).__name__}")
                continue
            if expected_type == "number" and not isinstance(value, (int, float)):
                errors.append(f"{name}: expected number, got {type(value).__name__}")
                continue
            if expected_type == "string" and not isinstance(value, str):
                errors.append(f"{name}: expected string, got {type(value).__name__}")
                continue
            if "min" in spec and value < spec["min"]:
                errors.append(f"{name}: {value} < min {spec['min']}")
            if "max" in spec and value > spec["max"]:
                errors.append(f"{name}: {value} > max {spec['max']}")
        return errors

    @abstractmethod
    def initialize(
        self,
        params: dict[str, Any],
        instrument: str,
        session_date: datetime,
    ) -> State:
        """Build a fresh per-session State from validated parameters."""

    @abstractmethod
    def on_bar(
        self,
        state: State,
        bar: Bar,
        features: FeatureSnapshot,
    ) -> list[Order]:
        """Process a new bar. Return zero or more Orders to submit.

        Stop-loss orders managed by the harness's stop class do not need
        to be re-emitted here; the strategy emits ENTRY orders.
        """

    def on_fill(self, state: State, fill: Fill) -> list[Order]:
        """Process a fill. Default: no-op (harness updates State.position
        directly). Override for order chaining (e.g., place stop after
        entry filled) when the strategy class wants direct control."""
        return []

    def on_exit(self, state: State, reason: ExitReason) -> list[Order]:
        """External exit command (stop hit, time stop, override). Default:
        no-op (the harness emits the closing market order itself)."""
        return []


StrategyClassFactory = Callable[[], StrategyClass]

_REGISTRY: dict[str, StrategyClassFactory] = {}


def register_strategy_class(
    name: str,
) -> Callable[[type[StrategyClass]], type[StrategyClass]]:
    """Decorator: register a StrategyClass subclass under `name`.

    The factory produced is the class itself (no-arg `__init__`); each
    `get_strategy_class` call returns a fresh instance because the class
    must be stateless per session (per the contract above).
    """

    def deco(cls: type[StrategyClass]) -> type[StrategyClass]:
        if name in _REGISTRY:
            raise ValueError(f"strategy class {name!r} already registered")
        if cls.id != name:
            raise ValueError(
                f"register_strategy_class({name!r}) but class.id == {cls.id!r}"
            )
        _REGISTRY[name] = cls
        return cls

    return deco


def get_strategy_class(name: str) -> StrategyClass:
    if name not in _REGISTRY:
        raise KeyError(
            f"strategy class {name!r} not registered; known: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name]()


def list_strategy_classes() -> list[str]:
    return sorted(_REGISTRY)
