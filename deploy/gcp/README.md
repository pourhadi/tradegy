# GCP deployment package — MES 0DTE paper trading

This package deploys the MES 0DTE paper daemon as a scheduled workload on a
single Compute Engine VM. The VM must also run IB Gateway paper mode with API
port `4002` reachable on `127.0.0.1`.

Do not deploy this to Cloud Run. The workload needs a persistent broker
session, local socket access to IB Gateway, shared on-disk state between entry
and management runs, and operator kill-switch control.

## Contents

| Path | Purpose |
|---|---|
| `Dockerfile` | Locked Python 3.12/uv runtime for `scripts/live_mes_0dte.py` |
| `deploy/gcp/cloudbuild.yaml` | Builds and pushes the app image to Artifact Registry |
| `deploy/gcp/env/mes-0dte.env.example` | Environment template for `/etc/tradegy/mes-0dte.env` |
| `deploy/gcp/env/ib-gateway.env.example` | Environment template for unattended IB Gateway + IBC |
| `deploy/gcp/bin/materialize-ib-gateway-secrets.sh` | Pulls IBKR credentials from Secret Manager into `/run/tradegy` |
| `deploy/gcp/systemd/tradegy-ib-gateway.service` | Persistent IB Gateway paper session |
| `deploy/gcp/systemd/*.service` | One-shot Docker services for entry and management |
| `deploy/gcp/systemd/*.timer` | America/New_York market-session schedules |

## Build image

Create the Artifact Registry repo once:

```bash
gcloud artifacts repositories create tradegy \
    --repository-format=docker \
    --location=us-central1
```

Build and push:

```bash
gcloud builds submit \
    --config deploy/gcp/cloudbuild.yaml \
    --substitutions _REGION=us-central1,_REPOSITORY=tradegy,_IMAGE_NAME=tradegy-mes-0dte
```

## VM contract

The VM should be private except for IAP SSH. Required host paths:

```bash
sudo mkdir -p /etc/tradegy \
    /var/lib/tradegy/data/live_options \
    /var/lib/tradegy/ib-gateway/settings \
    /opt/tradegy \
    /usr/local/lib/tradegy
```

Install Docker and authenticate Artifact Registry on the VM:

```bash
gcloud auth configure-docker us-central1-docker.pkg.dev
```

Copy the environment templates and helper:

```bash
sudo cp deploy/gcp/env/mes-0dte.env.example /etc/tradegy/mes-0dte.env
sudo cp deploy/gcp/env/ib-gateway.env.example /etc/tradegy/ib-gateway.env
sudo cp deploy/gcp/bin/materialize-ib-gateway-secrets.sh \
    /usr/local/lib/tradegy/materialize-ib-gateway-secrets.sh
sudo chmod 0600 /etc/tradegy/mes-0dte.env
sudo chmod 0600 /etc/tradegy/ib-gateway.env
sudo chmod 0755 /usr/local/lib/tradegy/materialize-ib-gateway-secrets.sh
```

Edit `/etc/tradegy/mes-0dte.env` to set the pushed `TRADEGY_IMAGE`, and edit
`/etc/tradegy/ib-gateway.env` only if the default Secret Manager names or IB
Gateway image need changing.

## Secret Manager

Create the required secrets once:

```bash
gcloud secrets create tradegy-ibkr-username --replication-policy=automatic
gcloud secrets create tradegy-ibkr-password --replication-policy=automatic
gcloud secrets create tradegy-ib-gateway-vnc-password --replication-policy=automatic
```

Add secret values locally. These commands prompt via stdin and keep the values
out of shell history if you paste after the command starts:

```bash
gcloud secrets versions add tradegy-ibkr-username --data-file=-
gcloud secrets versions add tradegy-ibkr-password --data-file=-
gcloud secrets versions add tradegy-ib-gateway-vnc-password --data-file=-
```

Grant the VM service account read access to those secrets:

```bash
gcloud secrets add-iam-policy-binding tradegy-ibkr-username \
    --member="serviceAccount:VM_SERVICE_ACCOUNT" \
    --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding tradegy-ibkr-password \
    --member="serviceAccount:VM_SERVICE_ACCOUNT" \
    --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding tradegy-ib-gateway-vnc-password \
    --member="serviceAccount:VM_SERVICE_ACCOUNT" \
    --role="roles/secretmanager.secretAccessor"
```

Copy systemd units:

```bash
sudo cp deploy/gcp/systemd/tradegy-* /etc/systemd/system/
sudo systemctl daemon-reload
```

## Unattended IB Gateway

The Gateway service uses `ghcr.io/gnzsnz/ib-gateway:stable`, which runs IB
Gateway under IBC in a headless container. Credentials are pulled from Secret
Manager at service start, written to `/run/tradegy`, and mounted read-only into
the container.

Start Gateway:

```bash
sudo systemctl enable --now tradegy-ib-gateway.service
sudo journalctl -u tradegy-ib-gateway.service -f
```

Required API behavior:

| Setting | Value |
|---|---|
| Account | paper account, e.g. `DU7535411` |
| API socket clients | enabled |
| Read-only API | off |
| Host-mapped socket port | `127.0.0.1:4002` |
| Trusted IP | `127.0.0.1` |
| Order precautions | bypass API prompts |

The Gateway container maps paper API traffic from host `127.0.0.1:4002` to the
container's paper socket. The app container runs with `--network host`, so
`IBKR_HOST=127.0.0.1` remains correct.

IBKR may still require 2FA on first login or after a trust/session reset. Access
the Gateway UI through an IAP SSH tunnel to localhost VNC if needed:

```bash
gcloud compute ssh VM_NAME --zone ZONE -- -L 5900:127.0.0.1:5900
```

Then connect a VNC client to `127.0.0.1:5900` using the VNC password stored in
`tradegy-ib-gateway-vnc-password`. Do not expose VNC publicly.

## Smoke tests

Pull the configured image:

```bash
source /etc/tradegy/mes-0dte.env
sudo docker pull "$TRADEGY_IMAGE"
```

Verify Gateway is listening on the host paper port:

```bash
sudo systemctl status tradegy-ib-gateway.service --no-pager
```

Verify the app boots without submitting an order:

```bash
sudo docker run --rm --network host \
    --env-file /etc/tradegy/mes-0dte.env \
    --volume /var/lib/tradegy:/var/lib/tradegy \
    "$TRADEGY_IMAGE" --dry-run
```

During market hours, this should connect to IB Gateway, qualify MES and the same
day option legs, fetch live quotes, and stop before submission.

Run management once:

```bash
sudo systemctl start tradegy-mes-manage.service
sudo journalctl -u tradegy-mes-manage.service -n 100 --no-pager
```

If there is no entry record for today, management should exit cleanly with
`no entry record ... nothing to manage`.

## Enable timers

```bash
sudo systemctl enable --now tradegy-mes-entry.timer
sudo systemctl enable --now tradegy-mes-manage.timer
systemctl list-timers 'tradegy-mes-*'
```

Schedules are market-local:

| Timer | Schedule |
|---|---|
| `tradegy-mes-entry.timer` | Mon-Thu 10:00 ET |
| `tradegy-mes-manage.timer` | Mon-Thu 10:15-15:45 ET every 15 min |

The daemon force-closes any still-open position at/after 15:30 ET.

## Operator controls

Pause new entries and force-close any open position on the next management tick:

```bash
sudo touch /var/lib/tradegy/data/live_options/MES_0DTE_KILL
```

Resume:

```bash
sudo rm /var/lib/tradegy/data/live_options/MES_0DTE_KILL
```

Inspect state:

```bash
sudo ls -la /var/lib/tradegy/data/live_options/mes_0dte_entries
sudo journalctl -u tradegy-mes-entry.service -n 200 --no-pager
sudo journalctl -u tradegy-mes-manage.service -n 200 --no-pager
sudo journalctl -u tradegy-ib-gateway.service -n 200 --no-pager
```

## Roll forward / rollback

Roll forward by updating `TRADEGY_IMAGE` in `/etc/tradegy/mes-0dte.env`, pulling
the image, and restarting no service. The next timer invocation uses the new
image.

Rollback by setting `TRADEGY_IMAGE` back to the prior digest or tag. Prefer a
digest for production paper trading once a smoke-tested image is selected.
