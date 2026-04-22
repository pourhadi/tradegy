# Strategy Definition

**Status:** Draft for review
**Purpose:** Define precisely what a "strategy" is in this system. Establish the identity rules that prevent fake library diversity. Specify the five components every strategy must declare. Distinguish strategies from strategy classes.

---

## The definition

**A strategy is a deterministic, parameterized procedure that, given market data and a context, produces a fully-specified trade lifecycle: when to enter, how much to size, where to exit, and under what conditions to abandon the trade.**

Unpacking:

- **Deterministic.** Same inputs produce the same outputs. No randomness, no discretion, no LLM-in-the-loop at the mechanical level. Replayable against history with bit-exact results.
- **Parameterized.** It has knobs. Same strategy at different settings is not two strategies.
- **Full trade lifecycle.** Entry, sizing, stops, exits, invalidation. An entry rule alone is not a strategy.
- **Conditional on context.** Declared conditions under which it's meant to run. Outside those conditions, it should not be activated. Context conditions are part of identity, not afterthought.

---

## The four tests

A candidate must pass all four to be called a strategy:

**1. The mechanical test.** A computer can execute it end-to-end without asking a human anything. If any step involves "trader evaluates," "LLM decides," or "depending on feel," it's not a strategy — it's a heuristic.

**2. The replay test.** Given the same market data twice, produces identical trades. If not, there's hidden state or unseeded randomness.

**3. The expectancy test.** Positive expected value, after realistic costs, in a definable set of market conditions. An entry rule with no edge isn't a strategy — it's a trade trigger.

**4. The falsifiability test.** You can state, in advance, the conditions under which this strategy would be considered broken and retired. Without falsifiability you'll keep trading it past its expiration.

Things that fail these tests and therefore are NOT strategies:

- "Trade the open" — fails mechanical and expectancy
- "Buy when RSI < 30" — fails full-lifecycle
- "Follow the trend" — fails mechanical
- "Fade overreactions" — fails all four
- "Let the LLM decide when to enter" — fails mechanical by construction

---

## The five components

Every library strategy must crisply declare:

**1. The edge statement.** One sentence naming the inefficiency being exploited. If you can't say it in a sentence, you don't know it.

Good: "Failed breakouts of the opening range in balanced sessions tend to revert because the initial move lacks institutional commitment."
Bad: "Price action around the opening range has tradeable patterns."

**2. The mechanism.** Why does the edge exist? What structural or behavioral feature causes it? This is what separates a real strategy from a backtest artifact.

Good: "Retail momentum chasers drive the initial break; institutional liquidity providers fade it when volume doesn't confirm commitment."
Bad: "It worked in backtest."

**3. The trigger.** The mechanical, observable-in-real-time condition that initiates a trade.

**4. The exit logic.** Profit targets, stops, invalidation conditions, time stops. All mechanical.

**5. The context envelope.** The conditions under which the edge is expected to exist — and the conditions under which it's expected to fail.

Strategies missing any of these five don't enter the library. The edge statement and mechanism are the most commonly skipped and the most load-bearing long-term, because they're what tells you whether a drawdown is "edge is gone" vs "normal variance."

---

## Identity: when are two strategies the same strategy?

**Two strategies are the same strategy if they exploit the same underlying market inefficiency using the same causal mechanism, even if their mechanical rules differ.**

Applications of the rule:

- `range_break_fade` at 15-min window and `range_break_fade` at 45-min window: **same strategy**, different parameters. Same library entry with different parameter values within the declared envelope.
- `orb_breakout` and `orb_fade`: **same edge viewed from opposite sides** — both exploit the opening range's information content. Related variants. `incompatible_with` prevents simultaneous activation.
- `vwap_reversion` and `range_break_fade`: **different strategies**. Different causal stories, different conditions, different failure modes.
- `range_break_fade` with volume confirmation and `range_break_fade` with delta confirmation: **variants of the same strategy**. Shared parent ID, distinct child IDs. Correlated but not identical failure modes.

**Why the rule matters:** it prevents fake diversity. If "same edge, different parameters" counts as separate strategies, the selection layer will activate three of them on the same day believing it's diversifying, and take correlated losses when the underlying edge fails. True library diversity is about diverse *edges*, not diverse specs.

---

## Strategy vs strategy class

Terminology distinction the system depends on:

- **Strategy class:** a code-level implementation. The `range_break_fade` Python class with its parameter contract. Registered in the strategy class registry (see `03_strategy_class_registry.md`).

- **Strategy:** a specific instantiation — `range_break_fade` with specific parameters, specific context envelope, full lifecycle specified, trading a specific instrument. Registered as a library entry conforming to the strategy spec schema (see `04_strategy_spec_schema.md`).

One class can back multiple library strategies. Example: two different context/parameter configurations of `range_break_fade` — one for low-vol mornings, one for high-vol — share the implementation but are separate library entries with separate evidence, separate context conditions, separate performance tracking.

**The code lives in the class. The contract is the strategy spec.** The library is a collection of specs. The registry is a collection of classes.

---

## What a strategy does at runtime

A strategy is a state machine. At any moment it is in exactly one state:

- **DORMANT** — not eligible. Outside time window, or context conditions unmet, or disabled. Does nothing.
- **ARMED** — eligible, watching for trigger. Consuming features, checking conditions each bar. No position.
- **ENTERING** — trigger fired, order working. Can cancel back to ARMED if trigger invalidates pre-fill.
- **IN_POSITION** — filled, managing stops/exits/invalidation. Most work happens here.
- **EXITING** — exit condition fired, exit order working.
- **DONE_FOR_SESSION** — completed allowed attempts or hit session-terminal condition.

All transitions are deterministic given market data. No runtime discretion.

---

## What a strategy does not do

- **Decide whether to run today.** That's the selection layer. The strategy has *declared* conditions; the selection layer *reads* them and decides.
- **Know about other strategies.** Isolated state machine. Coordination is the execution layer's job.
- **Manage account-level risk.** It only enforces its own risk envelope. Account caps are enforced above it.
- **Adapt its parameters.** Parameters fixed for the session once loaded. Adaptation happens between sessions through library governance.
- **Have discretion.** Trigger fires, it enters. Exit condition fires, it exits. The LLM can *override* via supervisory interface, but that's the LLM acting on the strategy, not the strategy acting with judgment.
- **Learn.** Within a session, no belief updates. Any "learning" happens offline between sessions.

**One exception:** strategy-level hard exclusions. Blackout windows (FOMC, CPI, etc.) are enforced at the strategy level as safety guards, not applicability judgments. Defense in depth — if the selection layer mistakenly activates during FOMC, the strategy itself refuses to fire.

General principle: **context conditions for applicability live at the selection layer. Context conditions for absolute safety live at the strategy layer.**

---

## What a successful strategy looks like

Quantitative characteristics required for live library inclusion:

- **Positive expectancy after realistic costs.** Commissions, slippage, data fees, margin costs all netted. Gross-positive, net-negative strategies do not enter. Ever.
- **Sharpe meaningfully above 1 in intended conditions.** Below 1, the risk-adjusted return can't survive normal drawdowns. Top-tier strategies land 1.5–2.5 in their intended context; above 3 is probably overfit or hiding tail risk.
- **Drawdown profile we can live with.** For $10k/MES: max DD < 15% of account, losing streak < 6 trades, recovery time < 30 trades. Hitting these triggers review.
- **In-sample/out-of-sample consistency.** Sharpe within ~30% across the divide. If in-sample is 2.5 and out-of-sample is 0.3, it's overfit regardless of interim live results.
- **Predictable behavior within envelope.** Takes trades when conditions match its spec; stands down otherwise. Unexpected activation or skipping indicates problems.
- **Graceful failure modes.** Small bounded losses when wrong. No catastrophic tails that dwarf typical wins.
- **Decorrelation with existing library.** Adds risk-adjusted return at the portfolio margin, not redundancy.
- **Mechanism still true.** The causal story behind the edge is still operative in current markets.

Three-question summary for ongoing success:

1. Is it making money we can explain?
2. Are the losses within the shape we expected?
3. Is it earning its library slot given what else exists?

No to any of these = strategy has stopped being successful, regardless of lifetime P&L.

---

## What a successful strategy is NOT

- **Not supposed to produce huge returns.** Individual strategies are solid, boring, repeatable edges. Large returns come from portfolio construction and selection quality.
- **Not supposed to work everywhere.** Universal applicability is a red flag for overfitting or dishonesty. Specialization is the feature.
- **Not supposed to be exciting.** Backtest results that look thrilling usually indicate overfitting or hidden tail risk. Boring is professional.
- **Not supposed to last forever.** Finite lifespans are expected. Retiring gracefully when the mechanism fails is success.

---

## The compressed definition

**A successful strategy is a repeatable conversion of a specific market pattern into positive risk-adjusted return, whose success we can explain, whose failures we can recognize, and whose conditions we can identify — with none of those four things being handwavy.**

- Can't explain why → can't trust it.
- Can't recognize when it's broken → will trade it past its grave.
- Can't identify its conditions → will deploy in wrong contexts.

Any of these handwavy = not a strategy, just temporarily lucky.
