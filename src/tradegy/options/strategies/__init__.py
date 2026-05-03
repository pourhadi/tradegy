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
from tradegy.options.strategies.iron_condor import IronCondor45dteD16

__all__ = [
    "IronCondor45dteD16",
]
