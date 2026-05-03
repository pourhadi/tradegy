"""Strategy registry for the options layer.

Maps strategy id strings (e.g. "put_credit_spread_45dte_d30") to
default-constructed `OptionStrategy` instances. CLI commands and
ad-hoc scripts use this to resolve user-supplied strategy ids
without each having to import each class by hand.

Per `14_options_volatility_selling.md` Phase B-2: registration is
declarative — every concrete class in `tradegy.options.strategies`
is registered here at module import time. Adding a new class means
adding a single line below.
"""
from __future__ import annotations

from tradegy.options.strategies import (
    CallCreditSpread45dteD30,
    CallDiagonal30_60,
    IronButterfly45dteAtm,
    IronCondor45dteD16,
    JadeLizard45dte,
    PutBrokenWingButterfly45dte,
    PutCalendar30_60AtmDeb,
    PutCreditSpread45dteD30,
    PutDiagonal30_60,
    ReverseIronCondor45dteD30,
    ShortStrangleDefined45dteD25,
)
from tradegy.options.strategy import OptionStrategy


_STRATEGY_FACTORIES: dict[str, type[OptionStrategy]] = {
    "call_credit_spread_45dte_d30": CallCreditSpread45dteD30,
    "call_diagonal_30_60_d30_d10": CallDiagonal30_60,
    "iron_butterfly_45dte_atm": IronButterfly45dteAtm,
    "iron_condor_45dte_d16": IronCondor45dteD16,
    "jade_lizard_45dte": JadeLizard45dte,
    "put_broken_wing_butterfly_45dte_d20": PutBrokenWingButterfly45dte,
    "put_calendar_30_60_atm_deb": PutCalendar30_60AtmDeb,
    "put_credit_spread_45dte_d30": PutCreditSpread45dteD30,
    "put_diagonal_30_60_d30_d10": PutDiagonal30_60,
    "reverse_iron_condor_45dte_d30": ReverseIronCondor45dteD30,
    "short_strangle_defined_45dte_d25": ShortStrangleDefined45dteD25,
}


def list_strategy_ids() -> list[str]:
    """All registered concrete strategy ids, in alphabetical order."""
    return sorted(_STRATEGY_FACTORIES.keys())


def get_strategy(strategy_id: str) -> OptionStrategy:
    """Construct a default instance of the strategy with `strategy_id`.

    Raises KeyError with the available ids on miss — no fallback.
    """
    if strategy_id not in _STRATEGY_FACTORIES:
        raise KeyError(
            f"unknown options strategy id {strategy_id!r}. "
            f"Registered: {list_strategy_ids()}"
        )
    return _STRATEGY_FACTORIES[strategy_id]()


def resolve_strategy_ids(spec: str) -> list[OptionStrategy]:
    """Comma-separated list of ids → list of constructed strategies.

    Accepts both single ids and comma-joined portfolios:

      "put_credit_spread_45dte_d30"
        → [PutCreditSpread45dteD30()]
      "put_credit_spread_45dte_d30,iron_condor_45dte_d16"
        → [PutCreditSpread45dteD30(), IronCondor45dteD16()]

    Whitespace around commas is tolerated. Empty / duplicate ids
    raise — no silent dedup.
    """
    parts = [s.strip() for s in spec.split(",") if s.strip()]
    if not parts:
        raise ValueError("strategy spec is empty")
    if len(parts) != len(set(parts)):
        raise ValueError(
            f"duplicate strategy id in {spec!r}; portfolio ids must "
            "be distinct"
        )
    return [get_strategy(p) for p in parts]
