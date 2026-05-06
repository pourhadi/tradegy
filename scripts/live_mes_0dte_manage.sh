#!/usr/bin/env bash
# 0DTE intraday-management wrapper — invoked at 15-min cadence
# (10:45, 11:00, 11:15, ..., 15:45 ET) by launchd.
#
# Sources ~/.zprofile and runs `live_mes_0dte.py --manage`.  Each
# invocation pulls live quotes for the four leg contracts, computes
# MTM vs entry credit, and triggers a profit-take close if MTM ≥
# 50% of credit.
#
# Cheap to run repeatedly — no-ops if there's no entry record for
# today or if the position has already been closed.

set -euo pipefail

REPO_ROOT="${REPO_ROOT:-/Users/dan/code/tradegy}"
LOG_DIR="${LOG_DIR:-${REPO_ROOT}/data/live_options/mes_0dte_logs}"
mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/$(date +%Y-%m-%d)_manage.log"

if [ -f "${HOME}/.zprofile" ]; then
    set +u
    source "${HOME}/.zprofile"
    set -u
fi

cd "${REPO_ROOT}"

echo "[$(date)] === live_mes_0dte MANAGE ===" | tee -a "${LOG_FILE}"

notify() {
    local title="$1"
    local body="$2"
    if command -v osascript >/dev/null 2>&1; then
        osascript -e "display notification \"${body}\" with title \"${title}\"" \
            >/dev/null 2>&1 || true
    fi
}

if ! uv run python "${REPO_ROOT}/scripts/live_mes_0dte.py" --manage \
        2>&1 | tee -a "${LOG_FILE}"; then
    rc=$?
    notify "tradegy MES 0DTE manage FAILED" "exit ${rc} — see ${LOG_FILE}"
    exit ${rc}
fi

if grep -q "PROFIT TAKE TRIGGERED" "${LOG_FILE}" 2>/dev/null \
        && ! grep -q "PROFIT TAKE TRIGGERED.*already" "${LOG_FILE}" 2>/dev/null; then
    notify "tradegy MES 0DTE profit-take" "$(date +%H:%M) — closing position"
fi
