# GCP MES 0DTE Paper Deployment

Status: **Phase 1 packaged** on 2026-05-10.

This document defines the first deployable GCP package for the MES 0DTE paper
trading daemon. It is intentionally a Compute Engine VM deployment, not Cloud
Run, because the system needs a persistent IB Gateway paper session and local
socket connectivity to the broker API.

## Deployed Strategy

| Field | Value |
|---|---|
| Script | `scripts/live_mes_0dte.py` |
| Strategy | MES 0DTE iron condor, $10/$10 offsets, $10 wings |
| Gate | none, daily-fire Mon-Thu |
| Profit take | 75% of entry credit |
| Force close | 15:30 ET |
| Sizing | 1 contract |
| Broker | IBKR paper via IB Gateway port `4002` |

The deployed spec is the validated daily-fire variant from doc 15. Changing the
offsets, wings, gate, profit take, or sizing requires a fresh research record and
held-out validation before deployment.

## Package Contents

| Artifact | Purpose |
|---|---|
| `Dockerfile` | Python 3.12 app image with locked `uv` dependencies |
| `.dockerignore` | Keeps local data, tests, docs, caches, and secrets out of the image context |
| `deploy/gcp/cloudbuild.yaml` | Cloud Build image build/push definition |
| `deploy/gcp/env/mes-0dte.env.example` | VM environment template for image, state path, timezone, and IBKR API values |
| `deploy/gcp/systemd/tradegy-mes-entry.*` | One-shot entry service and Mon-Thu 10:00 ET timer |
| `deploy/gcp/systemd/tradegy-mes-manage.*` | One-shot management service and 15-minute Mon-Thu 10:15-15:45 ET timer |
| `deploy/gcp/README.md` | Operator install, smoke-test, kill-switch, log, and rollback runbook |

## Runtime Contract

The VM owns the broker session and persistent state:

| Host path | Purpose |
|---|---|
| `/etc/tradegy/mes-0dte.env` | Image reference and runtime environment; populated from Secret Manager |
| `/var/lib/tradegy/data/live_options/mes_0dte_entries/` | Entry and close records shared by entry/manage invocations |
| `/var/lib/tradegy/data/live_options/MES_0DTE_KILL` | Operator kill switch |
| `journald` | Entry and management logs |

The container uses host networking. With IB Gateway running on the same VM,
`IBKR_HOST=127.0.0.1` is correct from inside the container.

## Code Changes Made For GCP

The daemon now supports explicit runtime paths and market timezone:

| Env var | Default | Purpose |
|---|---|---|
| `TRADEGY_DATA_DIR` | `<repo>/data` locally, `/var/lib/tradegy/data` in Docker | Root for runtime data |
| `TRADEGY_LIVE_OPTIONS_DIR` | `$TRADEGY_DATA_DIR/live_options` | Entry records, logs, kill switch |
| `TRADEGY_MARKET_TZ` | `America/New_York` | Session-date and force-close clock |

This removes the previous dependency on the VM/container host timezone and the
repo-local `data/live_options` path. The code still defaults to the original
repo-local path for local macOS operation.

## Scheduler

Systemd timers replace launchd on GCP:

| Timer | Schedule | Action |
|---|---|---|
| `tradegy-mes-entry.timer` | Mon-Thu 10:00 ET | Run `python scripts/live_mes_0dte.py` |
| `tradegy-mes-manage.timer` | Mon-Thu 10:15-15:45 ET every 15 min | Run `python scripts/live_mes_0dte.py --manage` |

`Persistent=false` is intentional. The system must not backfill missed trading
runs after VM downtime or a suspended timer; missed sessions are skipped rather
than replayed.

## Controls

Kill switch:

```bash
sudo touch /var/lib/tradegy/data/live_options/MES_0DTE_KILL
```

Resume:

```bash
sudo rm /var/lib/tradegy/data/live_options/MES_0DTE_KILL
```

Manual entry smoke test during market hours:

```bash
sudo docker run --rm --network host \
    --env-file /etc/tradegy/mes-0dte.env \
    --volume /var/lib/tradegy:/var/lib/tradegy \
    "$TRADEGY_IMAGE" --dry-run
```

Logs:

```bash
sudo journalctl -u tradegy-mes-entry.service -n 200 --no-pager
sudo journalctl -u tradegy-mes-manage.service -n 200 --no-pager
```

## Open Phase 2 Items

These are not required for the first paper deployment but should be added before
unattended live-money operation:

- Terraform for VM, service account, Artifact Registry IAM, Secret Manager IAM,
  persistent disk, and firewall/IAP policy.
- Cloud Monitoring alerts for failed entry/manage services, missing management
  runs after an entry, IB Gateway downtime, and still-open position after 15:45
  ET.
- Secret Manager bootstrap instead of manually writing
  `/etc/tradegy/mes-0dte.env`.
- IB Gateway supervisor hardening and daily restart verification.
