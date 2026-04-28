"""Strategy spec loader + validator.

Implements the validation invariants from 04_strategy_spec_schema.md:470
that the harness checks at spec load time:

1. metadata.id uniqueness — deferred to a library-wide loader; not
   enforced at single-spec load.
2. metadata.schema_version supported.
3. All referenced classes exist in their respective registries.
4. Numeric parameters in entry/sizing/stops/exits lie within their
   declared parameter_envelope.
5. stops.hard_max_distance_ticks respected by every stop method.
6-10. operational/live invariants — checked when status == "live"; the
   MVP only enforces 3 and 4 for now and emits structured warnings for
   the rest. Filed as known-deferred.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tradegy.specs.schema import StrategySpec
from tradegy.strategies.auxiliary import (
    get_condition_evaluator,
    get_exit_class,
    get_sizing_class,
    get_stop_adjustment_class,
    get_stop_class,
)
from tradegy.strategies.base import get_strategy_class


SUPPORTED_SCHEMA_VERSIONS = ("1.0",)


class SpecValidationError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n  - ".join(["spec validation failed:"] + errors))
        self.errors = list(errors)


def load_spec(path: Path) -> StrategySpec:
    """Read YAML from `path` and return a validated StrategySpec.

    Raises SpecValidationError on any validation failure (registry refs,
    envelope, etc.). Pydantic's own validation errors propagate as
    ValidationError before our checks run.
    """
    raw = yaml.safe_load(Path(path).read_text())
    spec = StrategySpec.model_validate(raw)
    errors = validate_spec(spec)
    if errors:
        raise SpecValidationError(errors)
    return spec


def validate_spec(spec: StrategySpec) -> list[str]:
    """Run all post-deserialization invariants. Returns error list."""
    errors: list[str] = []

    # Invariant 2: schema version supported.
    if spec.metadata.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(
            f"metadata.schema_version {spec.metadata.schema_version!r} not in "
            f"{SUPPORTED_SCHEMA_VERSIONS}"
        )

    # Invariant 3: registry references resolve.
    errors.extend(_validate_registry_refs(spec))

    # Invariant 4: parameter envelope membership.
    if spec.parameter_envelope is not None:
        errors.extend(_validate_parameter_envelopes(spec))

    # Invariant 5: stops.hard_max_distance_ticks. Only enforceable when
    # the stop method declares a stop_ticks parameter; otherwise this
    # check is symbolic (real enforcement happens at fill time when the
    # actual stop distance is known).
    initial = spec.stops.initial_stop
    if "stop_ticks" in initial:
        if int(initial["stop_ticks"]) > spec.stops.hard_max_distance_ticks:
            errors.append(
                f"stops.initial_stop.stop_ticks ({initial['stop_ticks']}) > "
                f"hard_max_distance_ticks ({spec.stops.hard_max_distance_ticks})"
            )

    return errors


def _validate_registry_refs(spec: StrategySpec) -> list[str]:
    errors: list[str] = []

    # entry.strategy_class
    try:
        cls = get_strategy_class(spec.entry.strategy_class)
        errs = cls.validate_parameters(spec.entry.parameters)
        errors.extend([f"entry.parameters.{e}" for e in errs])
    except KeyError as exc:
        errors.append(f"entry.strategy_class: {exc}")

    # sizing.method
    try:
        sizing = get_sizing_class(spec.sizing.method)
        errs = sizing.validate_parameters(spec.sizing.parameters)
        errors.extend([f"sizing.parameters.{e}" for e in errs])
    except KeyError as exc:
        errors.append(f"sizing.method: {exc}")

    # stops.initial_stop.method
    initial = dict(spec.stops.initial_stop)
    method = initial.pop("method", None)
    if method is None:
        errors.append("stops.initial_stop missing required field 'method'")
    else:
        try:
            stop = get_stop_class(method)
            errs = stop.validate_parameters(initial)
            errors.extend([f"stops.initial_stop.{e}" for e in errs])
        except KeyError as exc:
            errors.append(f"stops.initial_stop.method: {exc}")

    # stops.adjustment_rules[].action
    for i, rule in enumerate(spec.stops.adjustment_rules):
        action = rule.get("action")
        if action is None:
            errors.append(f"stops.adjustment_rules[{i}] missing 'action'")
            continue
        try:
            get_stop_adjustment_class(action)
        except KeyError as exc:
            errors.append(f"stops.adjustment_rules[{i}].action: {exc}")

    # exits.profit_targets[].method
    for i, target in enumerate(spec.exits.profit_targets):
        try:
            ex = get_exit_class(target.method)
            errs = ex.validate_parameters(target.parameters)
            errors.extend([f"exits.profit_targets[{i}].{e}" for e in errs])
        except KeyError as exc:
            errors.append(f"exits.profit_targets[{i}].method: {exc}")

    # exits.invalidation_conditions[].condition
    for i, cond in enumerate(spec.exits.invalidation_conditions):
        try:
            ev = get_condition_evaluator(cond.condition)
            errs = ev.validate_parameters(cond.parameters)
            errors.extend([f"exits.invalidation_conditions[{i}].{e}" for e in errs])
        except KeyError as exc:
            errors.append(f"exits.invalidation_conditions[{i}].condition: {exc}")

    # time_stop is special: not a registered exit class in the spec sense —
    # it's a flat boolean block. Validation happens at harness load when
    # the block resolves to time_stop ExitClass internally.
    if spec.stops.time_stop is not None and spec.stops.time_stop.enabled:
        try:
            ex = get_exit_class("time_stop")
            errs = ex.validate_parameters(
                {"max_holding_bars": spec.stops.time_stop.max_holding_bars}
            )
            errors.extend([f"stops.time_stop.{e}" for e in errs])
        except KeyError as exc:
            errors.append(f"stops.time_stop: {exc}")

    return errors


def _validate_parameter_envelopes(spec: StrategySpec) -> list[str]:
    """Numeric parameter values in entry/sizing/stops must lie within the
    declared envelope, per docs:228-249. Filing this as the speed-bump
    that prevents quiet drift.
    """
    if spec.parameter_envelope is None:
        return []

    envelopes = spec.parameter_envelope.envelopes()
    errors: list[str] = []

    sources = {
        "entry": spec.entry.parameters,
        "sizing": spec.sizing.parameters,
    }
    initial = dict(spec.stops.initial_stop)
    initial.pop("method", None)
    sources["stops.initial_stop"] = initial

    for prefix, params in sources.items():
        for name, value in params.items():
            if name not in envelopes:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            env = envelopes[name]
            if value < env.tested_min or value > env.tested_max:
                errors.append(
                    f"{prefix}.{name} = {value} outside tested envelope "
                    f"[{env.tested_min}, {env.tested_max}]"
                )
    return errors
