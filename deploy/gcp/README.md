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
sudo mkdir -p /etc/tradegy /var/lib/tradegy/data/live_options /opt/tradegy
```

Install Docker and authenticate Artifact Registry on the VM:

```bash
gcloud auth configure-docker us-central1-docker.pkg.dev
```

Copy the environment template to `/etc/tradegy/mes-0dte.env`, fill the image
and IBKR account values from Secret Manager, and keep the file mode restricted:

```bash
sudo cp deploy/gcp/env/mes-0dte.env.example /etc/tradegy/mes-0dte.env
sudo chmod 0600 /etc/tradegy/mes-0dte.env
```

Copy systemd units:

```bash
sudo cp deploy/gcp/systemd/tradegy-mes-* /etc/systemd/system/
sudo systemctl daemon-reload
```

## IB Gateway requirement

IB Gateway must be logged into the paper account before the timers run.

Required API settings:

| Setting | Value |
|---|---|
| Account | paper account, e.g. `DU7535411` |
| API socket clients | enabled |
| Read-only API | off |
| Socket port | `4002` |
| Trusted IP | `127.0.0.1` |
| Order precautions | bypass API prompts |

The app container runs with `--network host`, so `IBKR_HOST=127.0.0.1` means the
Gateway process on the VM host.

## Smoke tests

Pull the configured image:

```bash
source /etc/tradegy/mes-0dte.env
sudo docker pull "$TRADEGY_IMAGE"
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
```

## Roll forward / rollback

Roll forward by updating `TRADEGY_IMAGE` in `/etc/tradegy/mes-0dte.env`, pulling
the image, and restarting no service. The next timer invocation uses the new
image.

Rollback by setting `TRADEGY_IMAGE` back to the prior digest or tag. Prefer a
digest for production paper trading once a smoke-tested image is selected.
