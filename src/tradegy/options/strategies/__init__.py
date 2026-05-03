"""Vol-selling strategy classes.

Per `14_options_volatility_selling.md` Phase C scope:
  - iron_condor (delta-anchored entry + delta-anchored wings)
  - put_credit_spread (directional, defined-risk)
  - short_strangle_defined (narrow body + wider wings)
  - calendar_spread (term-structure capture)

Phase B-2 ships the iron condor as the first concrete strategy so
the runner has something to backtest end-to-end. The other three
classes follow once B-2 has shipped.
"""
from tradegy.options.strategies.call_credit_spread import CallCreditSpread45dteD30
from tradegy.options.strategies.iron_butterfly import IronButterfly45dteAtm
from tradegy.options.strategies.iron_condor import IronCondor45dteD16
from tradegy.options.strategies.iv_gated import IvGatedStrategy
from tradegy.options.strategies.jade_lizard import JadeLizard45dte
from tradegy.options.strategies.put_broken_wing_butterfly import (
    PutBrokenWingButterfly45dte,
)
from tradegy.options.strategies.put_calendar import PutCalendar30_60AtmDeb
from tradegy.options.strategies.put_credit_spread import PutCreditSpread45dteD30
from tradegy.options.strategies.put_diagonal import PutDiagonal30_60
from tradegy.options.strategies.short_strangle_defined import (
    ShortStrangleDefined45dteD25,
)

__all__ = [
    "CallCreditSpread45dteD30",
    "IronButterfly45dteAtm",
    "IronCondor45dteD16",
    "IvGatedStrategy",
    "JadeLizard45dte",
    "PutBrokenWingButterfly45dte",
    "PutCalendar30_60AtmDeb",
    "PutCreditSpread45dteD30",
    "PutDiagonal30_60",
    "ShortStrangleDefined45dteD25",
]
