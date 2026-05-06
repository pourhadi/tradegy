#!/usr/bin/env bash
# 0DTE entry wrapper — invoked weekdays at 10:25 ET by launchd.
#
# Sources ~/.zprofile so IBKR_PAPER_ACCOUNT, IBKR_HOST, IBKR_PORT
# are visible under launchd (no interactive shell).  Then runs
# `live_mes_0dte.py` (entry mode) and tees output to a per-day log.
#
# Exit codes:
#   0  success (gate may not have fired — that's fine)
#   1  generic error (vix lookup failed, etc.)
#   2  IB Gateway connection failed
#   3  underlying-price snapshot failed
#   4  strike construction failed
#   5  chain-snapshot for legs failed (couldn't quote a leg)
#   6  combo placement rejected by IBKR

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/Users/dan/code/tradegy}"
LOG_DIR="${LOG_DIR:-${REPO_ROOT}/data/live_options/mes_0dte_logs}"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/$(date +%Y-%m-%d)_entry.log"

if [ -f "${HOME}/.zprofile" ]; then
    set +u
    source "${HOME}/.zprofile"
    set -u
fi

cd "${REPO_ROOT}"

today=$(date +%Y-%m-%d)
echo "[$(date)] === live_mes_0dte ENTRY ${today} ===" | tee -a "${LOG_FILE}"

notify() {
    local title="$1"
    local body="$2"
    if command -v osascript >/dev/null 2>&1; then
        osascript -e "display notification \"${body}\" with title \"${title}\"" \
            >/dev/null 2>&1 || true
    fi
}
trap 'rc=$?; if [ $rc -ne 0 ]; then notify "tradegy MES 0DTE FAILED" "exit ${rc} on ${today} — see ${LOG_FILE}"; fi' EXIT

if ! uv run python "${REPO_ROOT}/scripts/live_mes_0dte.py" \
        2>&1 | tee -a "${LOG_FILE}"; then
    rc=$?
    echo "[$(date)] FAIL exit ${rc}" | tee -a "${LOG_FILE}"
    exit ${rc}
fi

echo "[$(date)] === DONE ${today} ===" | tee -a "${LOG_FILE}"

# Notify when an entry actually fired (vs gate-skip silence).
if grep -q "combo placed" "${LOG_FILE}" 2>/dev/null; then
    notify "tradegy MES 0DTE entered" "${today}: combo placed (paper)"
elif grep -q "VIX gate not passing" "${LOG_FILE}" 2>/dev/null; then
    : # silent on gate-skip — too noisy to notify on no-trade days
fi
