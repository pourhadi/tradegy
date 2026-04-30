"""Auxiliary class registries: sizing, stop, stop adjustment, exit,
condition evaluator.

Per 03_strategy_class_registry.md:147-214, strategy classes compose with
auxiliary classes to produce a strategy spec. Each auxiliary type has:

- An ABC declaring the contract.
- A registration decorator + lookup, mirroring the strategy class /
  transform / live adapter pattern.
- A common parameter-schema-based `validate_parameters` (same shape as
  StrategyClass).

Why five? Each axis (sizing, stop, stop adjustment, exit, condition) is
independently composable: a `range_break_fade` strategy might use
`fixed_fractional_risk` sizing + `opposite_range_extreme` stop +
`r_multiple_target` exit. The spec wires them together; the harness
resolves them at load time.

Conditions are special — they're boolean predicates over the feature
stream, used for context filters and invalidation checks. They get the
same registry mechanics for uniformity.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Generic, TypeVar

from tradegy.strategies.types import (
    Bar,
    FeatureSnapshot,
    Position,
    Side,
    State,
)


# ---------------------------------------------------------------------------
# common parameter validator (DRY across all auxiliary ABCs)
# ---------------------------------------------------------------------------


def _validate_against_schema(
    params: dict[str, Any], schema: dict[str, dict[str, Any]]
) -> list[str]:
    errors: list[str] = []
    for name, spec in schema.items():
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
        if "min" in spec and value < spec["min"]:
            errors.append(f"{name}: {value} < min {spec['min']}")
        if "max" in spec and value > spec["max"]:
            errors.append(f"{name}: {value} > max {spec['max']}")
    return errors


# ---------------------------------------------------------------------------
# generic registry helper (used by all auxiliary types)
# ---------------------------------------------------------------------------

T = TypeVar("T")


class _Registry(Generic[T]):
    """Per-type lookup table. Each auxiliary kind owns one instance."""

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._table: dict[str, type[T]] = {}

    def register(self, name: str) -> Callable[[type[T]], type[T]]:
        def deco(cls: type[T]) -> type[T]:
            if name in self._table:
                raise ValueError(f"{self._kind} {name!r} already registered")
            if getattr(cls, "id", None) != name:
                raise ValueError(
                    f"register_{self._kind}({name!r}) but class.id == "
                    f"{getattr(cls, 'id', None)!r}"
                )
            self._table[name] = cls
            return cls

        return deco

    def get(self, name: str) -> T:
        if name not in self._table:
            raise KeyError(
                f"{self._kind} {name!r} not registered; "
                f"known: {sorted(self._table)}"
            )
        return self._table[name]()

    def list(self) -> list[str]:
        return sorted(self._table)


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------


class SizingClass(ABC):
    """How many contracts to enter, given desired risk."""

    id: str = "<unset>"
    version: str = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {}

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        return _validate_against_schema(params, self.parameter_schema)

    @abstractmethod
    def size(
        self,
        params: dict[str, Any],
        intended_side: Side,
        entry_price: float,
        stop_price: float,
        account_equity: float,
    ) -> int:
        """Return the integer number of contracts to enter (always >= 0)."""


_sizing_registry: _Registry[SizingClass] = _Registry("sizing_class")


def register_sizing_class(name: str):
    return _sizing_registry.register(name)


def get_sizing_class(name: str) -> SizingClass:
    return _sizing_registry.get(name)


def list_sizing_classes() -> list[str]:
    return _sizing_registry.list()


# ---------------------------------------------------------------------------
# Initial stop
# ---------------------------------------------------------------------------


class StopClass(ABC):
    """How to place the initial stop price for an entry."""

    id: str = "<unset>"
    version: str = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {}

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        return _validate_against_schema(params, self.parameter_schema)

    @abstractmethod
    def stop_price(
        self,
        params: dict[str, Any],
        side: Side,
        entry_price: float,
        bar: Bar,
        features: FeatureSnapshot,
    ) -> float:
        """Return the absolute price level for the initial stop."""


_stop_registry: _Registry[StopClass] = _Registry("stop_class")


def register_stop_class(name: str):
    return _stop_registry.register(name)


def get_stop_class(name: str) -> StopClass:
    return _stop_registry.get(name)


def list_stop_classes() -> list[str]:
    return _stop_registry.list()


# ---------------------------------------------------------------------------
# Stop adjustment (post-entry stop modification)
# ---------------------------------------------------------------------------


class StopAdjustmentClass(ABC):
    """How to adjust an existing stop in flight (move-to-breakeven, trail, etc.)."""

    id: str = "<unset>"
    version: str = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {}

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        return _validate_against_schema(params, self.parameter_schema)

    @abstractmethod
    def adjusted_stop(
        self,
        params: dict[str, Any],
        position: Position,
        bar: Bar,
        features: FeatureSnapshot,
    ) -> float | None:
        """Return new stop price, or None to keep current."""


_stop_adj_registry: _Registry[StopAdjustmentClass] = _Registry("stop_adjustment_class")


def register_stop_adjustment_class(name: str):
    return _stop_adj_registry.register(name)


def get_stop_adjustment_class(name: str) -> StopAdjustmentClass:
    return _stop_adj_registry.get(name)


def list_stop_adjustment_classes() -> list[str]:
    return _stop_adj_registry.list()


# ---------------------------------------------------------------------------
# Exit (profit target / time stop / invalidation)
# ---------------------------------------------------------------------------


class ExitClass(ABC):
    """When to close a position. Returns True to flatten now."""

    id: str = "<unset>"
    version: str = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {}

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        return _validate_against_schema(params, self.parameter_schema)

    @abstractmethod
    def should_exit(
        self,
        params: dict[str, Any],
        position: Position,
        bar: Bar,
        features: FeatureSnapshot,
    ) -> bool:
        """Return True if the position should be closed at this bar."""


_exit_registry: _Registry[ExitClass] = _Registry("exit_class")


def register_exit_class(name: str):
    return _exit_registry.register(name)


def get_exit_class(name: str) -> ExitClass:
    return _exit_registry.get(name)


def list_exit_classes() -> list[str]:
    return _exit_registry.list()


# ---------------------------------------------------------------------------
# Condition evaluator (boolean predicate for context_conditions / invalidation)
# ---------------------------------------------------------------------------


class ConditionEvaluator(ABC):
    """Boolean predicate over (bar, features, position, params)."""

    id: str = "<unset>"
    version: str = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {}

    def validate_parameters(self, params: dict[str, Any]) -> list[str]:
        return _validate_against_schema(params, self.parameter_schema)

    @abstractmethod
    def evaluate(
        self,
        params: dict[str, Any],
        bar: Bar,
        features: FeatureSnapshot,
        position: Position,
    ) -> bool:
        """Return True if the condition holds at this bar."""


_cond_registry: _Registry[ConditionEvaluator] = _Registry("condition_evaluator")


def register_condition_evaluator(name: str):
    return _cond_registry.register(name)


def get_condition_evaluator(name: str) -> ConditionEvaluator:
    return _cond_registry.get(name)


def list_condition_evaluators() -> list[str]:
    return _cond_registry.list()
