"""HealthCheck ABC + registry.

A HealthCheck is a small object with a single `check(now)` method that
returns a HealthCheckResult. The runner calls every registered check
on each tick and routes results through the alert router.

Checks are intentionally lightweight — most are just a comparison of
a measured value against a configured threshold. The framework does
NOT enforce any cadence (the runner does); checks are pure functions
of their inputs at the call instant.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime

from tradegy.monitoring.types import HealthCheckResult


class HealthCheck(ABC):
    """One health-check implementation.

    Subclasses implement `check(now)`, returning a HealthCheckResult.
    The `id` class attribute is used in result.check_id and as the
    base for dedup keys. Construction takes whatever inputs the check
    needs (injected, not auto-discovered).
    """

    id: str = "<override>"

    @abstractmethod
    def check(self, *, now: datetime) -> HealthCheckResult:
        """Evaluate the check at `now`. Return a result with severity OK
        if the check passed, otherwise the appropriate severity.

        Implementations must NOT block; if a check needs to call an
        async resource, capture a snapshot in the constructor and
        evaluate that snapshot here. The runner is the orchestration
        boundary for I/O.
        """


CheckFactory = Callable[[], HealthCheck]
_REGISTRY: dict[str, CheckFactory] = {}


def register_check(name: str) -> Callable[[CheckFactory], CheckFactory]:
    """Decorator to register a HealthCheck factory under a string name.

    Mirrors the live-adapter / transform / strategy-class registry
    pattern — name → no-arg factory that returns a fresh instance.
    """

    def deco(factory: CheckFactory) -> CheckFactory:
        if name in _REGISTRY:
            raise ValueError(f"health check {name!r} already registered")
        _REGISTRY[name] = factory
        return factory

    return deco


def get_check(name: str) -> HealthCheck:
    if name not in _REGISTRY:
        raise KeyError(
            f"health check {name!r} not registered; known: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[name]()


def list_checks() -> list[str]:
    return sorted(_REGISTRY)
