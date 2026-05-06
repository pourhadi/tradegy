#!/usr/bin/env bash
# Launch the MES 0DTE live paper-trading dashboard in a browser.
#
# Reads from the daemon's on-disk artifacts (no IBKR connection
# needed).  Auto-refreshes every 30 sec.  Default port is 8765;
# override with first arg.
#
# Usage:
#   ./scripts/mes_0dte_dashboard.sh              # port 8765
#   ./scripts/mes_0dte_dashboard.sh 9000         # port 9000

set -euo pipefail

PORT="${1:-8765}"
REPO_ROOT="${REPO_ROOT:-/Users/dan/code/tradegy}"

cd "${REPO_ROOT}"
echo "Launching MES 0DTE dashboard on http://localhost:${PORT}"
echo "Press Ctrl-C to stop."

exec uv run streamlit run \
    "${REPO_ROOT}/scripts/mes_0dte_dashboard.py" \
    --server.port "${PORT}" \
    --server.headless false
