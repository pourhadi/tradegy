"""fixed_contracts — fixed N contracts per trade, ignoring stop distance.

The simplest sizing class. Useful as the MVP default and for strategies
where risk-budgeting is handled outside the spec (e.g., a discretionary
external risk envelope). Per 03_strategy_class_registry.md:155.
"""
from __future__ import annotations

from typing import Any

from tradegy.strategies.auxiliary import SizingClass, register_sizing_class
from tradegy.strategies.types import Side


@register_sizing_class("fixed_contracts")
class FixedContracts(SizingClass):
    id = "fixed_contracts"
    version = "v1"
    parameter_schema: dict[str, dict[str, Any]] = {
        "contracts": {"type": "integer", "min": 1, "max": 10, "default": 1},
    }

    def size(
        self,
        params: dict[str, Any],
        intended_side: Side,
        entry_price: float,
        stop_price: float,
        account_equity: float,
    ) -> int:
        return int(params.get("contracts", 1))
