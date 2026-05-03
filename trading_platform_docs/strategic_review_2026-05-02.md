# Strategic Review — 2026-05-02

**Status:** Snapshot, not a spec. Captures a step-back analysis after
the auto-gen scanner Phase 1 verification batch killed three
consecutive regime-anchored fade hypotheses (cumulative 15 killed
variants, 0 survivors).

**Why this exists:** Easy to keep adding "one more scanner" or "one
more feature" instead of asking whether the approach is converging.
This file is a forcing function: revisit it before the next
hypothesis-system buildout to check whether the priors that justified
that work are still standing.

---

## Verdict

Viable in principle, but the current method is failing in a way the
infrastructure cannot fix.

We've built ~70% of a serious platform. We have 0% of the alpha.
Fifteen killed hypotheses across four sprint rounds, zero survivors,
~10,000 simulated trades all losing money after costs. The platform
is solid (data pipeline with no-lookahead validation, walk-forward +
CPCV + holdout, execution layer with FSM + IBKR + reconciliation,
monitoring, anti-overfitting discipline, evidence packets, 402 tests).
The signal isn't.

---

## The structural problem nobody named yet

**LLM ideation has a fundamental selection bias toward arbed-away
patterns.** The hypotheses Claude generates — gap fade, OR breakout,
vol compression, exhaustion fade, lunchtime fade — are exactly the
canonical patterns that fill quant-trading textbooks. If any of them
were profitable, they'd already be arbed away. The LLM's training
corpus is dense on what *was* tradeable in 1995–2015 and effectively
empty on what isn't yet public. That's not a "more scanners" problem.
That's a "the hypothesis source is structurally compromised" problem.

**Three consecutive fade kills in the regime-anchored batch is
informative.** Combined with the prior 12, this is now strong
evidence that single-instrument MES at 1m bars with price-and-volume-
only features doesn't contain the kind of edge an LLM can spot.
That's a real finding. Most retail-accessible intraday futures alpha
comes from things we don't have: order flow / DOM / footprint, cross-
asset signals (VIX, bonds, DXY), macro context, statistical
relationships across correlated instruments, or execution edges
(queue position, smart routing).

---

## What's actually missing (load-bearing for the goal)

| Item | Status | Load-bearing for |
|---|---|---|
| Cross-asset features (VIX, bonds, sector ETFs) | parked (Round 4) | search-space expansion |
| Live data ingest (rolling state / event-driven materialization) | not built | paper / live trading |
| Statistical anomaly detector | not built | breaking the LLM-ideation loop |
| Portfolio-level risk + selection layer (doc 09) | docs only | running >1 strategy |
| Paper trading loop | execution exists, no paper wiring | trust-before-live |
| Adversarial null tests (random-shuffle features, scrambled labels) | not built | methodology validation |
| Order flow / DOM / footprint features | not built | most retail-accessible intraday alpha |
| Sub-1m features | not built | execution-as-edge |

---

## What you might be missing

1. **Profitable retail-accessible intraday systematic strategies are
   *hard*.** Professional shops typically have at least one of: order
   flow data, latency advantages, capital scale, market-making
   infrastructure. A single-trader auto-discovery system is at a
   structural disadvantage you cannot engineer around with more code.

2. **Sunk-cost fallacy is the relevant risk now.** We've built a lot.
   It is tempting to keep adding "one more scanner" — that is
   confirmation that the problem is "tooling," when it might be
   "search method" or "search space."

3. **Higher-timeframe strategies (15m / 1h / daily) tend to have more
   persistent edges.** "Intraday only" might be optimizing for a
   regime where retail systematic edges are thinnest.

4. **Execution edge vs. signal edge.** Most retail-accessible alpha
   is in execution, not signals. Our execution layer is built for
   "place market orders when signal fires" — not for execution-as-
   edge.

---

## What to change (ranked, three concrete shifts)

1. **Pivot hypothesis sourcing.** Stop using LLM as primary ideator
   (0/15 hit rate). Build a statistical anomaly detector first; let
   *data* find candidate patterns, then use LLM only to articulate
   them. This is the highest-leverage change and the cheapest one we
   haven't tried.

2. **Unblock cross-asset inputs at any cost.** Even $50/mo for Cboe
   Datashop is small relative to the search-space expansion. Three
   intraday-MES-only fade kills are saying the search space here is
   genuinely thin.

3. **Set a kill date for the LLM-ideation hypothesis.** After N=20
   more kills with no survivor, declare that approach falsified and
   move to a different method (data-mined anomalies + literature-
   driven hypotheses, or change the goal entirely).

---

## The goal-framing question

> Is the goal "make money on MES intraday" or "build a system that
> makes money trading futures"? If the latter, MES intraday is one
> path among many — and probably not the most tractable one.

This is *the* prior worth re-examining before more buildout. If "MES
intraday" is a hard constraint (e.g. you trade discretionarily in
that regime and want a system to augment that), the buildout
continues but with eyes open about the structural disadvantage. If
"MES intraday" was a starting point chosen for tractability rather
than necessity, it is worth questioning, because most of what we have
built is instrument- and timeframe-agnostic.

What "MES intraday" implicitly constrains:

- **Single instrument** → no pairs / baskets / spreads / cross-asset
  divergence plays. The richest classes of retail-accessible
  systematic edges (calendar spreads, ES/NQ/RTY rotation, bond/
  equity correlation breaks, sector ETF baskets) are foreclosed.
- **Intraday only** → trades open and close within the same session.
  This is the most-watched, lowest-decay regime — where competition
  is densest and edges are thinnest. Higher timeframes (15m / 1h /
  daily / multi-day swing) have more persistent edges precisely
  because they require less infrastructure to compete in.
- **Implicitly directional** → "make money on MES" means betting on
  direction. Non-directional plays (vol selling on options on
  futures, calendar arbitrage, market-making) need different goal
  framing entirely but reuse much of the same infrastructure.

What lifting each constraint would buy:

- **Multi-instrument intraday** (ES + NQ + RTY + ZN + ZB + GC + CL):
  the platform mostly already supports this; data sources and
  feature catalogs need expanding, but the harness and execution
  layer don't change. Cross-asset divergence and spread-mean-
  reversion are real, well-documented retail-accessible edges.
- **Higher-timeframe MES** (15m–daily holds): the harness already
  supports it; the strategy classes mostly already support it.
  Macro / term-structure / event-driven plays open up. Costs per
  trade matter less because moves are bigger.
- **Different objective** (calendar spreads, vol selling, basket
  hedging): the data pipeline + harness reuse; execution layer
  needs minor extensions. The advantage: these aren't competing
  with high-frequency directional algos.

The cost of asking this question now is low. The cost of *not*
asking it after 15+ more kills is high.

---

## Execution edge vs. signal edge

> Most retail-accessible alpha is in execution, not signals. Our
> execution layer is built for "place market orders when signal
> fires" — not for execution-as-edge.

A "signal edge" is: there exists some predictor `f(features)` such
that `E[returns | f(features)] > costs`. Find the predictor, fire a
market order, take the move. This is what every strategy in our
repo currently looks like. It is also where retail systematic
trading is structurally weakest, because the signal space is small,
public, and densely competed.

An "execution edge" is: given a need to transact (your own or
someone else's), capture the bid-ask spread or queue position or
short-term order-book imbalance. The "signal" is microstructural,
not predictive. Examples retail can plausibly play in:

- **Limit-ladder market making.** Quote both sides of the inside
  market with a tiny lean toward the side with thinner book; collect
  spread - adverse-selection cost. Edge is structural (spread is
  real), not predictive. Profit per round is tiny but trade count
  is enormous.
- **Queue-position arbitrage.** Place limit orders earlier in the
  queue at price levels that get touched repeatedly during chop;
  capture spread on fills, cancel on adverse moves. Requires good
  cancel/replace latency and a fee structure that doesn't penalize
  it.
- **Maker-rebate capture.** Some venues pay a rebate to passive
  liquidity. If your fills cluster on the rebate side, the rebate
  itself is the edge.
- **Aggressive-fade after sweep.** When a large market order
  sweeps multiple price levels, the post-sweep prints often mean-
  revert as overreacting flow exits. Catch the bounce with a limit
  ladder. Requires DOM data we don't have.
- **Implementation shortfall reduction.** If the *signal* is small
  but real (e.g. an arbitrage between MES and SPY ETF basket),
  execution quality determines whether it's profitable. A 0.3-tick
  signal becomes profitable iff slippage is also 0.3 ticks.

Why our current execution layer doesn't capture any of this:

- **Order types are coarse**: market and stop. No limit-order ladder
  primitives. No queue-position model. No cancel/replace state
  machine optimized for quote churn.
- **Position model is unidirectional**: we hold a directional
  position with one initial stop. Market-making implies both-sided
  inventory and asymmetric exit logic.
- **Cost model assumes passive doesn't matter**: 0.5 ticks/side
  slippage applied to every fill. A maker fill on a passive limit
  is *not* 0.5 ticks of slippage — it can be a *credit*. The
  current cost model can't even represent the edge.
- **Bar-driven timing**: orders fire at bar open after a 1m signal.
  Execution edges live at the millisecond / order-event level.
- **Feature panel is bar-derived**: no DOM snapshot, no trade-tape
  imbalance, no inside-quote duration. The features that *signal*
  execution opportunities are missing.

What pivoting toward execution-edge would mean:

- **Different data**: order book (L2 / L3) and trade tape, not just
  OHLCV. IBKR provides this; databento sells it; both add cost and
  storage.
- **Different harness**: event-driven simulation (per-message
  replay, queue position tracking, cancel/replace latency model),
  not bar-driven. This is a non-trivial harness rewrite.
- **Different cost model**: maker / taker fees per venue, rebates,
  realistic queue-position fill probabilities, latency between
  decision and ack.
- **Different strategy classes**: limit-ladder, mean-reversion-in-
  the-spread, post-sweep-fade. Existing classes don't generalize.

Why this matters for the goal-framing question:

If the long-term goal is "make money trading futures with code,"
execution-edge strategies are a parallel path with a different
risk/reward profile than signal-edge strategies. Their edges tend
to be smaller per trade but more numerous and more persistent
(microstructure decays slower than alpha). They are also less
crowded by other retail systematic traders because the
infrastructure barrier is higher.

Worth noting: this is not an "instead of" — it is an "or also."
Many successful retail systematic traders run both: a small set of
signal-edge strategies for direction + an execution layer that
captures spread on the way in and out, so the directional signal
clears costs more easily.

---

## Where things are in the development flow

**Built:** data pipeline (Stages 1–7), backtest harness (single +
walk-forward + CPCV + holdout), execution layer (FSM + IBKR +
reconciliation), live monitoring framework (Phase 1), auto-gen
pipeline (Phases A+B+C), scanner Phase 1 (kill log + market scan),
holdout-in-auto-test, 402 tests, anti-overfitting discipline,
evidence packets, ATR-cap prompt rule.

**Not built (load-bearing):** live data ingest, paper trading loop,
portfolio risk, selection layer (doc 09), cross-asset data sources,
sub-1m / order flow features, macro event calendar, statistical
anomaly detector, embedding-diversity hypothesis dedup, Deflated
Sharpe Ratio, adversarial null tests.

**Have zero of:** profitable strategies, paper-traded strategies,
live-traded strategies.

---

## Decision points to revisit at the next pinning

1. Is "MES intraday" still the goal, or is it a starting constraint
   we should relax?
2. Has the LLM-ideation approach produced a survivor in the next N
   tries? If not at N=20 cumulative kills, the prior on the method
   is dead.
3. Has the statistical-anomaly-detector path been tried? If not, it
   is the highest-leverage unexplored move.
4. Is cross-asset data unblocked? If still parked, signal-search is
   running in a strict subset of the addressable space.
