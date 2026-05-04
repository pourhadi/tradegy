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


# 30-DTE variants of the validated PCS+IC+JL portfolio. Same delta
# anchors as 45-DTE versions; only target_dte differs. Faster
# cycling for capital-constrained accounts where trade frequency
# matters more than per-trade theta capture.
#
# Each factory returns a fresh instance with the appropriate id
# overridden so portfolio-mode runs can mix 30-DTE and 45-DTE
# variants without id collision.
def _pcs_30dte() -> "PutCreditSpread45dteD30":
    return PutCreditSpread45dteD30(
        target_dte=30, id="put_credit_spread_30dte_d30",
    )


def _ic_30dte() -> "IronCondor45dteD16":
    return IronCondor45dteD16(
        target_dte=30, id="iron_condor_30dte_d16",
    )


def _jl_30dte() -> "JadeLizard45dte":
    return JadeLizard45dte(
        target_dte=30, id="jade_lizard_30dte",
    )


# Factory functions for parameterized variants — keyed alongside
# the class-based factories. resolve_strategy_ids() falls back to
# these when an id isn't a known class.
_STRATEGY_PARAM_FACTORIES: dict[str, callable] = {
    "put_credit_spread_30dte_d30": _pcs_30dte,
    "iron_condor_30dte_d16": _ic_30dte,
    "jade_lizard_30dte": _jl_30dte,
}


def list_strategy_ids() -> list[str]:
    """All registered strategy ids — concrete classes + parameter
    factories, alphabetical."""
    return sorted(
        list(_STRATEGY_FACTORIES.keys())
        + list(_STRATEGY_PARAM_FACTORIES.keys())
    )


def get_strategy(strategy_id: str) -> OptionStrategy:
    """Construct a default instance of the strategy with `strategy_id`.

    Tries the class registry first (default-constructed instance);
    falls back to the parameter-factory registry. Raises KeyError
    with the available ids on miss — no fallback to a generic
    strategy.
    """
    if strategy_id in _STRATEGY_FACTORIES:
        return _STRATEGY_FACTORIES[strategy_id]()
    if strategy_id in _STRATEGY_PARAM_FACTORIES:
        return _STRATEGY_PARAM_FACTORIES[strategy_id]()
    raise KeyError(
        f"unknown options strategy id {strategy_id!r}. "
        f"Registered: {list_strategy_ids()}"
    )


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
