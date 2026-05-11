#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${1:-/etc/tradegy/ib-gateway.env}"
RUN_DIR="${TRADEGY_RUN_DIR:-/run/tradegy}"
SECRET_DIR="${RUN_DIR}/secrets"
DOCKER_ENV_FILE="${RUN_DIR}/ib-gateway-secrets.env"

if [ ! -f "${ENV_FILE}" ]; then
    printf 'ERROR: missing env file %s\n' "${ENV_FILE}" >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

required=(
    GCP_SECRET_IBKR_USERNAME
    GCP_SECRET_IBKR_PASSWORD
    GCP_SECRET_IBKR_VNC_PASSWORD
)
for name in "${required[@]}"; do
    if [ -z "${!name:-}" ]; then
        printf 'ERROR: %s is not set in %s\n' "${name}" "${ENV_FILE}" >&2
        exit 1
    fi
done

install -d -m 0700 "${SECRET_DIR}"

gcloud secrets versions access latest \
    --secret="${GCP_SECRET_IBKR_USERNAME}" \
    > "${SECRET_DIR}/tws_userid"
gcloud secrets versions access latest \
    --secret="${GCP_SECRET_IBKR_PASSWORD}" \
    > "${SECRET_DIR}/tws_password"
gcloud secrets versions access latest \
    --secret="${GCP_SECRET_IBKR_VNC_PASSWORD}" \
    > "${SECRET_DIR}/vnc_password"

chmod 0600 "${SECRET_DIR}"/*

{
    printf 'TWS_USERID=%s\n' "$(tr -d '\r\n' < "${SECRET_DIR}/tws_userid")"
    printf 'TWS_PASSWORD_FILE=/run/secrets/tws_password\n'
    printf 'VNC_SERVER_PASSWORD_FILE=/run/secrets/vnc_password\n'
} > "${DOCKER_ENV_FILE}"
chmod 0600 "${DOCKER_ENV_FILE}"
