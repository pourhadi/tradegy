#!/usr/bin/env bash
# Daily live-options paper-trade cron wrapper.
#
# Designed for launchd (or cron) — runs the full daily pipeline:
#   1. Health probe (TWS up? paper account reachable?)
#   2. Pull today's SPY chain from ORATS
#   3. Ingest into spy_options_chain
#   4. Generate decision + route to IBKR paper (entries + V2 close loop)
#
# Exit codes propagate so launchd can see what went wrong:
#   0 = success
#   2 = TWS connection failed (probe step)
#   3 = ORATS pull failed
#   4 = ingest failed
#   6 = routing failed (IBKR rejected one or more orders)
#   non-zero other = anything else
#
# Idempotent: re-running the same day is safe — ORATS download
# script supports --resume, ingest dedups by (ts_utc, expir_date,
# strike), and routed orders use idempotent client_order_ids that
# IBKR rejects on duplicate.
#
# Wires the validated config from doc 14 Phase D-8 follow-up #6:
# SPY + PCS+IC+JL + IV<0.25 + 50/21/200 mgmt + $25K capital.
#
# REQUIRED ENV:
#   ORATS_API_KEY        — set in ~/.zprofile
#   IBKR_PAPER_ACCOUNT   — your paper account (e.g. DU7535411)
#   IBKR_HOST            — defaults to 127.0.0.1 if unset
#   IBKR_PORT            — defaults to 7497 (paper TWS)
#   IBKR_CLIENT_ID       — defaults to 17

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/Users/dan/code/tradegy}"
DATA_REPO="${DATA_REPO:-/Users/dan/code/data}"
LOG_DIR="${LOG_DIR:-${REPO_ROOT}/data/live_options/cron_logs}"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/$(date +%Y-%m-%d).log"

# Source the user profile so PATH + ORATS_API_KEY are available
# under launchd (which runs without an interactive shell).
if [ -f "${HOME}/.zprofile" ]; then
    set +u  # zprofile may reference unset vars
    # shellcheck disable=SC1091
    source "${HOME}/.zprofile"
    set -u
fi

if [ -z "${ORATS_API_KEY:-}" ]; then
    echo "[$(date)] ERROR: ORATS_API_KEY not set" | tee -a "${LOG_FILE}"
    exit 1
fi
if [ -z "${IBKR_PAPER_ACCOUNT:-}" ]; then
    echo "[$(date)] ERROR: IBKR_PAPER_ACCOUNT not set" | tee -a "${LOG_FILE}"
    exit 1
fi

cd "${REPO_ROOT}"

today=$(date +%Y-%m-%d)
echo "[$(date)] === live_options_daily ${today} ===" | tee -a "${LOG_FILE}"

# macOS native notification on failure — no external service
# needed. Banner shows in Notification Center even if Terminal
# is closed. Caller passes (title, body); function tolerates
# missing osascript on non-macOS environments.
notify() {
    local title="$1"
    local body="$2"
    if command -v osascript >/dev/null 2>&1; then
        osascript -e "display notification \"${body}\" with title \"${title}\"" \
            >/dev/null 2>&1 || true
    fi
}
trap 'rc=$?; if [ $rc -ne 0 ]; then notify "tradegy live-options FAILED" "exit ${rc} on ${today} — see cron_logs/$(date +%Y-%m-%d).log"; fi' EXIT

# 1. Health probe.
echo "[$(date)] step 1/4 — IBKR health probe" | tee -a "${LOG_FILE}"
if ! uv run tradegy live-options-health \
        --paper-account "${IBKR_PAPER_ACCOUNT}" \
        2>&1 | tee -a "${LOG_FILE}"; then
    echo "[$(date)] FAIL: health probe" | tee -a "${LOG_FILE}"
    exit 2
fi

# 2. Pull today's SPY chain.
echo "[$(date)] step 2/4 — ORATS SPY pull (${today})" | tee -a "${LOG_FILE}"
if ! python "${DATA_REPO}/download_spx_options_orats.py" \
        --ticker SPY \
        --start "${today}" --end "${today}" \
        --confirm --resume \
        2>&1 | tee -a "${LOG_FILE}"; then
    echo "[$(date)] FAIL: ORATS pull" | tee -a "${LOG_FILE}"
    exit 3
fi

# 3. Ingest.
echo "[$(date)] step 3/4 — ingest" | tee -a "${LOG_FILE}"
if ! uv run tradegy ingest \
        "${DATA_REPO}/spy_options_orats/spy_options_orats.csv" \
        --source-id spy_options_chain \
        2>&1 | tee -a "${LOG_FILE}"; then
    echo "[$(date)] FAIL: ingest" | tee -a "${LOG_FILE}"
    exit 4
fi

# 4. Generate decision + route.
echo "[$(date)] step 4/4 — live-options --route" | tee -a "${LOG_FILE}"
if ! uv run tradegy live-options \
        --route --paper-account "${IBKR_PAPER_ACCOUNT}" \
        2>&1 | tee -a "${LOG_FILE}"; then
    rc=$?
    echo "[$(date)] FAIL: routing (exit ${rc})" | tee -a "${LOG_FILE}"
    exit ${rc}
fi

echo "[$(date)] === SUCCESS ${today} ===" | tee -a "${LOG_FILE}"

# Success notification — banner so the operator knows the cron
# ran without having to open the log. Mention entry/close counts
# extracted from the latest log lines so the banner is informative.
recent_summary=$(grep -E "(entry candidates|close routing results|NO ENTRIES)" \
    "${LOG_FILE}" 2>/dev/null | tail -2 | tr '\n' '|' || echo "ran")
notify "tradegy live-options ok" "${today}: ${recent_summary}"
