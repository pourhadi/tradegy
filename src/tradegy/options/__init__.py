"""Options on futures / equity-index — chain ingest, Greeks, IV surface.

Per `14_options_volatility_selling.md` Phase A. The module is the
options-side counterpart to the existing bar-stream feature pipeline.
Vendor-independent today (chain ingest is contract-only); ORATS /
CBOE / databento adapters land as separate sibling modules once a
data vendor is committed.

Public surface:

- `ChainSnapshot`, `OptionLeg`, `OptionSide` — chain-snapshot
  dataclasses, the unit of per-day options data the harness consumes.
- `bs_price`, `bs_greeks` — Black-Scholes price + Greeks for
  European-style options on a non-dividend-paying or continuous-
  dividend underlying. Vendor-independent and testable against
  textbook reference values; SPX is European-style so this covers
  the intended Phase B underlying without modification.
- `implied_vol` — Newton-Raphson IV solver from market price.

The Greeks computation is intentionally NOT delegated to the data
vendor: ORATS / CBOE both publish Greeks but with different model
choices (dividend handling, day-count, vol surface interpolation).
We compute Greeks ourselves so cross-vendor parity is decidable on
our terms, and so backtests are reproducible without a live vendor
subscription.
"""
from tradegy.options.chain import (
    ChainSnapshot,
    OptionLeg,
    OptionSide,
)
from tradegy.options.cost_model import OptionCostModel
from tradegy.options.positions import (
    LegOrder,
    MultiLegOrder,
    MultiLegPosition,
    OptionPosition,
    compute_max_loss_per_contract,
)
from tradegy.options.risk import (
    PortfolioGreeks,
    RiskConfig,
    RiskDecision,
    RiskManager,
    compute_portfolio_greeks,
)
from tradegy.options.runner import (
    ClosedTrade,
    OptionsBacktestResult,
    RejectedOrder,
    SnapshotPnL,
    run_options_backtest,
)
from tradegy.options.strategies import (
    CallCreditSpread45dteD30,
    IronButterfly45dteAtm,
    IronCondor45dteD16,
    JadeLizard45dte,
    PutCalendar30_60AtmDeb,
    PutCreditSpread45dteD30,
    ShortStrangleDefined45dteD25,
)
from tradegy.options.strategy import (
    ManagementRules,
    OptionStrategy,
    should_close,
)
from tradegy.options.chain_features import (
    atm_iv,
    expected_move_to_expiry,
    iv_percentile_252d,
    iv_rank_252d,
    put_call_skew_25d,
    realized_vol_30d,
    term_structure_slope,
)
from tradegy.options.chain_io import (
    iter_chain_snapshots,
    load_chain_frames,
)
from tradegy.options.greeks import (
    bs_greeks,
    bs_price,
    implied_vol,
    Greeks,
)

__all__ = [
    "CallCreditSpread45dteD30",
    "ChainSnapshot",
    "ClosedTrade",
    "Greeks",
    "IronButterfly45dteAtm",
    "IronCondor45dteD16",
    "JadeLizard45dte",
    "LegOrder",
    "ManagementRules",
    "MultiLegOrder",
    "MultiLegPosition",
    "OptionCostModel",
    "OptionLeg",
    "OptionPosition",
    "OptionSide",
    "OptionStrategy",
    "OptionsBacktestResult",
    "PortfolioGreeks",
    "PutCalendar30_60AtmDeb",
    "PutCreditSpread45dteD30",
    "RejectedOrder",
    "RiskConfig",
    "RiskDecision",
    "RiskManager",
    "ShortStrangleDefined45dteD25",
    "SnapshotPnL",
    "atm_iv",
    "bs_greeks",
    "bs_price",
    "compute_max_loss_per_contract",
    "compute_portfolio_greeks",
    "expected_move_to_expiry",
    "implied_vol",
    "iter_chain_snapshots",
    "iv_percentile_252d",
    "iv_rank_252d",
    "load_chain_frames",
    "put_call_skew_25d",
    "realized_vol_30d",
    "run_options_backtest",
    "should_close",
    "term_structure_slope",
]
