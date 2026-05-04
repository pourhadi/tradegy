# Operator runbook — live options paper-trading

This is the consolidated runbook for deploying the validated
options vol-selling config (`SPY + PCS+IC+JL + IV<0.25` per
[doc 14](14_options_volatility_selling.md) Phase D-8 follow-up
#6) to IBKR paper account on $25K capital.

**Start here every time you sit down with this system.** Then
follow the section that matches what you need to do.

---

## What's deployed

| Layer | Module | Status |
|---|---|---|
| Validated config | `SPY + PCS+IC+JL + IV<0.25 + 50/21/200 mgmt` | ✅ doc 14 §"Walk-forward + CPCV findings" |
| Daily decision generator | `tradegy live-options` (no-route mode) | ✅ V1 (commit 5d1b9cb) |
| IBKR routing | `tradegy live-options --route` | ✅ V1 + V2 + V3a |
| V2 reconciliation | broker-position fetch + matching | ✅ commit 6d559b0 |
| V2 close automation | should_close + close routing | ✅ commit 6d559b0 |
| V3a fill confirmation | poll until terminal state | ✅ commit e4f0cdd |
| Daily cron | launchd plist + wrapper script | ✅ commit 6d559b0 |
| macOS notifications | osascript banner on success/fail | ✅ |
| Health probe | `tradegy live-options-health` | ✅ |

---

## Day-zero install (do this once)

### 1. Required env in `~/.zprofile`

```bash
# ORATS Pro API key — already set if you've been pulling SPX/XSP
export ORATS_API_KEY=...

# IBKR paper account number (e.g. DU7535411)
export IBKR_PAPER_ACCOUNT=DU7535411

# Optional overrides — defaults shown
# export IBKR_HOST=127.0.0.1
# export IBKR_PORT=7497         # paper TWS; live TWS = 7496
# export IBKR_CLIENT_ID=17
```

Reload: `source ~/.zprofile`.

### 2. TWS / IB Gateway setup

In TWS:
- **File → Global Configuration → API → Settings**
  - ✅ Enable ActiveX and Socket Clients
  - ✅ Read-Only API: **OFF** (we need to place orders)
  - Socket port: **7497** (paper)
  - Master API client ID: blank
  - ✅ Trusted IPs: add `127.0.0.1`
- **File → Global Configuration → API → Precautions**
  - ✅ Bypass Order Precautions for API Orders: ON
  - (these prompts would block the cron otherwise)
- Login to your paper account (account starts with `DU…`)

For unattended operation: TWS supports auto-restart and
auto-login via the **Configuration → Lock and Exit** settings.
Set up a scheduled restart at 11:45 PM ET (30 min before TWS's
forced daily logout at midnight ET) so it's logged in by
9:30 AM the next day.

### 3. Verify the connection

```bash
uv run tradegy live-options-health --paper-account DU7535411
```

Expected output:
```
ibkr connected  host=127.0.0.1  port=7497  client_id=17
  managed_accounts=['DU7535411']
```

If you see `connection failed`: TWS isn't running, isn't logged
in, or the API port doesn't match. If you see `--paper-account
... not in managed accounts`: typo in the env var.

### 4. First decision (no route)

```bash
uv run tradegy live-options
```

This runs the full backtest replay to warm up IV-rank state, then
prints today's entry candidates (or "NO ENTRIES today") and writes
the JSON decision to `data/live_options/decisions/<date>_<ts>.json`.

Read the JSON to make sure the strategy is generating sensible
candidates against the latest ingested SPY chain.

### 5. Install the launchd cron (when you're ready)

```bash
cp scripts/com.tradegy.live-options.plist \
   ~/Library/LaunchAgents/com.tradegy.live-options.plist
launchctl load ~/Library/LaunchAgents/com.tradegy.live-options.plist
launchctl list | grep tradegy
```

The cron now runs **weekdays at 17:30 local time**. Each invocation:
1. Health probes IBKR
2. Pulls today's SPY chain from ORATS
3. Ingests into `spy_options_chain`
4. Generates decision + routes entries + closes via IBKR paper
5. Writes a per-day log to `data/live_options/cron_logs/<date>.log`
6. Pops a macOS notification banner (success or failure)

---

## Daily operation

### Normal session

You should see a "tradegy live-options ok" banner around 17:30
each weekday with a one-liner summary of entries / closes for
the day. If you see "tradegy live-options FAILED", check
`data/live_options/cron_logs/$(date +%Y-%m-%d).log` for the
failure step.

### What the system does each session

1. **Health probe** — TWS reachable + paper account in managed list?
   Fails fast with exit code 2 if not.
2. **Pull + ingest today's SPY chain** — appends one date partition
   under `data/raw/source=spy_options_chain/`.
3. **Replay all snapshots** — rebuilds the IvGatedStrategy's
   IV-rank history (252-day rolling window) so today's gate
   decision uses the same logic as the validated backtest.
4. **Reconcile broker positions vs registry** — see "What to do
   when reconciliation diverges" below.
5. **Evaluate `should_close`** for each registered position
   against today's snapshot. Routes any triggered closes
   (50% profit, 21 DTE, or 200% loss).
6. **Generate today's entries** — strategies' `on_chain` against
   the latest snapshot. If the IV-rank gate passes (rank < 0.25),
   each strategy may queue one combo order.
7. **Place each order** — multi-leg combo at the cost-model's
   net mid. Polls for FILLED state for up to 30 seconds; cancels
   if the limit doesn't fill in that window.
8. **Persist registry** for FILLED entries; persist `close` rows
   for FILLED closes.

### Where state lives

| Path | What |
|---|---|
| `data/live_options/decisions/<date>_<ts>.json` | one per session — what the strategy decided |
| `data/live_options/positions.jsonl` | append-only registry of `open` and `close` events |
| `data/live_options/cron_logs/<date>.log` | per-day cron output |
| `data/raw/source=spy_options_chain/` | ingested ORATS chain partitions |

---

## Troubleshooting

### "tradegy live-options FAILED" notification

Open the cron log for the day. The exit code in the FAILED line
tells you which step:

| Exit | Step | Likely cause |
|---|---|---|
| 2 | health probe | TWS down / not logged in / API port mismatch |
| 3 | ORATS pull | API key invalid / rate limit / network |
| 4 | ingest | corrupt CSV / disk full |
| 6 | routing | IBKR rejected an order (margin, contract qual, etc.) |

### Reconciliation divergence (closes paused)

The cron output table will show `local-only` or `broker-only`
rows. The cron WILL NOT auto-close anything when this happens —
it surfaces the divergence and lets you investigate.

**`local-only`**: registry says we hold a position but the broker
doesn't show all the legs. Common causes:
- You manually closed the position via TWS
- A leg was assigned (especially deep ITM short put near ex-div)
- Partial fill that ate one leg

Fix: append a `close` row to `positions.jsonl` manually with the
right `closed_reason` (e.g. `"manual_tws_close"`,
`"early_assignment"`), then re-run the cron.

**`broker-only`**: broker has option positions on SPY that aren't
in the registry. Common causes:
- You opened a position via TWS directly
- Pre-existing position from before this system was installed
- Registry write failure on a prior session

Fix: either close the broker position via TWS, or backfill the
registry by adding an `open` row matching the broker's legs
(get entry_price + entry_credit from your IBKR statement).

### Order didn't fill within timeout

The cron logs `error: order in working after fill-wait timeout —
cancelled`. The order was cancelled at the broker; nothing in the
registry. Next session will re-attempt with a fresh decision.

If this happens repeatedly: the cost-model's `spread_offset_
fraction=0.20` may be too aggressive for current market conditions.
Override on the next manual run:

```bash
uv run tradegy live-options --route --paper-account DU7535411 \
    --fill-timeout 60.0
```

If still not filling: the strategy's target deltas / wing widths
may not match the day's chain liquidity. This is an investigative
moment — pull the decision JSON and look at the proposed legs'
bid/ask.

### Cron didn't run

```bash
launchctl list | grep tradegy
# If empty: the plist isn't loaded.
launchctl load ~/Library/LaunchAgents/com.tradegy.live-options.plist

# Check launchd's own logs
tail -f data/live_options/cron_logs/launchd.err
```

Common cause: the plist references absolute paths to
`/Users/dan/code/tradegy/...`. If you've moved the repo, edit
the plist + reload.

### TWS auto-logout overnight

TWS forces a logout daily (default 23:55 ET; configurable to
either 23:55 ET or 07:00 ET in **Lock and Exit**). If the cron
runs at 17:30 the next day before you've manually re-logged-in,
the health probe fails.

Fix: set up TWS's **Auto Restart** in Lock and Exit. This
reconnects automatically using your saved credentials. Or use
IB Gateway (lighter-weight) which has the same setting.

---

## Modifying the deployed config

The CLI defaults match the validated config. To change anything,
override on the command line:

```bash
# tighter IV gate — IV<0.20 has higher Sharpe, fewer trades
uv run tradegy live-options --route --paper-account DU7535411 \
    --iv-gate-max 0.20

# tight management (25/21/200) instead of default 50/21/200
uv run tradegy live-options --route --paper-account DU7535411 \
    --profit-take 0.25

# different strategy mix (drop JL)
uv run tradegy live-options --route --paper-account DU7535411 \
    --strategy-ids put_credit_spread_45dte_d30,iron_condor_45dte_d16

# scale capital
uv run tradegy live-options --route --paper-account DU7535411 \
    --capital 50000
```

To make a change permanent for the cron, edit the wrapper script
`scripts/live_options_daily.sh` to pass the override flag.

**Important**: any change away from the validated config means
you're trading something that wasn't walk-forward validated. The
discipline gate's pass result is for the specific config in
[doc 14 §"Walk-forward survivors found via IV-gating"](14_options_volatility_selling.md).
Re-run the walk-forward sweep before deploying off-validated configs:

```bash
uv run tradegy options-walk-forward <strategy_ids> \
    --start 2020-01-01 --end 2026-01-01 --capital <new> \
    --iv-gate-max <new> --profit-take <new> --loss-stop <new>
```

---

## What's NOT yet automated (deferred items)

These are real gaps the operator should be aware of:

- **Notifications other than macOS banners** — no email/Slack/SMS.
  If macOS notification center is muted, you'll miss failures.
  Mitigation: `tail -f data/live_options/cron_logs/launchd.err`
  in a tmux session, or set up a 3rd-party tail-and-alert tool.
- **Dividend-aware Greeks** — BS Greeks in `tradegy.options.greeks`
  do NOT model dividends. SPY's quarterly dividends affect short-
  call early-assignment probability near ex-div. The 50%/21 DTE
  management discipline limits but does not eliminate this risk.
  Watch ex-div dates manually for now.
- **Registry backfill from existing positions** — if you opened
  SPY options outside this system before installing it, those
  show up as `broker-only` divergences. Manual JSONL editing as
  documented in the troubleshooting section.
- **Fill-confirmation polling** uses a fixed timeout. A
  smarter approach would re-quote and re-submit at a marginally
  worse price if the original limit doesn't fill. Not built;
  current behavior is "cancel and retry next session."

---

## When to stop / re-evaluate

The validation in doc 14 is on a 6-year history (2020-2025) with
2020-COVID heavily weighted in early IS windows. The discipline-
gated config delivered ~28% AnnRoC in OOS years (2023-2025). But:

- 6 years is short.
- The 2020-COVID regime is unusual.
- 2025 had unique tariff-driven vol; the 2026 regime may differ.

After **90 days of paper trading**, compare:
- Realized P&L vs walk-forward expectation (~$28K/yr on $25K
  scaled to 90 days = ~$7K)
- Win rate vs ~70-80% expected
- Worst single-day drawdown
- Reconciliation divergence frequency (should be near-zero)

If the live results are dramatically worse than the OOS
expectation, do NOT push to live $$$. Investigate what's
different about 2026 regime, or stand down.

If the live results align with expectations, live $$$ deployment
is the next conversation — not a code change.
