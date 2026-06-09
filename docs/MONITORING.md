# Grove Monitoring

Operator guide for the OpenObserve + Keep monitoring stack shipped in GATH-44.

> **Why this shape, not LGTM + Uptime Kuma?** The original GATH-44 spec called for the LGTM (Loki + Grafana + Tempo + Mimir) stack plus Uptime Kuma. After cost-conscious review (`grill-me` session 2026-06-09), we pivoted to OpenObserve (consolidates LGTM + Uptime Kuma + native alerting into one binary, AGPL-3.0 self-hosted, zero per-host pricing) and Keep (open-source AIOps alert routing). Discord webhooks replace Slack/PagerDuty for the solo-dev tier.

## Stack at a glance

| Service | Port (host) | What it does |
|---|---|---|
| **OpenObserve** | `5080` | Synthetic HTTP/TCP monitors, threshold alerts, logs/metrics/traces ingestion, dashboards. Parquet-on-MinIO storage. |
| **Keep backend** | `8080` | Alert dedup + severity-based routing. Receives OpenObserve webhooks, fires Discord. |
| **Keep frontend** | `3034` | UI for viewing alert state, manually firing test alerts, editing workflows. |

Both services join the existing `gatheratthegrove_internal` Docker network so OpenObserve can probe internal-only services (Postgres TCP) via service-name DNS.

## Quickstart

```bash
# 1. Configure
cp odoocker/.env.monitoring.example odoocker/.env.monitoring
# Edit .env.monitoring:
#   - Paste your Discord warning + critical webhook URLs
#   - Replace the dev-default Keep tokens with `openssl rand -hex 32`
#   - Rotate OPENOBSERVE_ROOT_PASSWORD

# 2. Bring up the main stack first (monitoring has nothing useful to monitor without it)
make all-up

# 3. Bring up monitoring + bootstrap configs in one command
make monitoring-up
```

After step 3:
- **OpenObserve UI**: http://localhost:5080 (log in with `OPENOBSERVE_ROOT_EMAIL` + password from `.env.monitoring`)
- **Keep UI**: http://localhost:3034 (no auth in dev mode)

The bootstrap script:
- Uploads 20 synthetic monitors (4 frontends × 1-3 routes + Odoo + 3 Ghosts + Postgres + 4 SSL placeholders)
- Uploads 7 alert rules with severity tags
- Uploads 1 status-page dashboard
- Configures Keep's 2 Discord providers + 3 workflows

Idempotent — re-run safe.

## Daily operations

| Task | Command |
|---|---|
| Check what's currently alerting | http://localhost:5080/web/alerts |
| Test the wiring end-to-end | `./scripts/smoke-test-monitoring.sh` |
| Update monitors after editing JSON | `make monitoring-setup` |
| Update Keep workflows after editing YAML | `make monitoring-setup` |
| View Discord routing config | http://localhost:3034/providers |
| Tail container logs | `make monitoring-logs` |
| Stop the stack (preserves Parquet) | `make monitoring-down` |

## Verifying the GATH-44 acceptance

```bash
./scripts/smoke-test-monitoring.sh
```

This script:
1. Sends a connectivity-check alert through Keep → Discord warning channel (confirms wiring works before any destructive action)
2. Captures pre-kill timestamp
3. `docker kill gatheratthegrove-odoo-1`
4. Polls OpenObserve for the `odoo-down-critical` alert to fire
5. Restores Odoo
6. Asserts: alert fired within 120 seconds

PASS → GATH-44 acceptance criterion verified. The Discord critical channel should contain an `@here` ping with a runbook link to `docs/RUNBOOKS.md#odoo-down`.

## Editing monitors

Monitors are config-as-code in `openobserve/monitors.json`. Schema:

```json
{
  "name": "unique-monitor-name",
  "type": "http" | "tcp" | "ssl",
  "url": "http://host:port/path",       // http only
  "host": "hostname", "port": 5432,     // tcp/ssl only
  "method": "GET" | "POST",             // http only
  "expected_status": 200,               // http only — exact match
  "expected_status_in": [200, 401],     // http only — set match
  "expected_keyword": "literal string", // http only — body contains
  "interval_seconds": 30,
  "timeout_seconds": 10,
  "alert_days_before_expiry": 14,       // ssl only
  "tags": ["tenant:X", "tier:Y", "service:Z"]
}
```

After editing, re-run `make monitoring-setup` to upsert.

## Editing alerts

Alerts are config-as-code in `openobserve/alerts.json`. Each alert references a monitor (or set of monitors) and a trigger condition. Severity (`warning` | `critical`) maps to Keep workflows (`workflows.yml`) which select the Discord channel.

After editing, `make monitoring-setup`.

## Editing Discord routing

Two layers:
1. **Providers** (`keep/providers.yml`) — which webhooks to call (warning channel, critical channel). Edit the channel→URL mapping here.
2. **Workflows** (`keep/workflows.yml`) — which severity goes to which provider, message template, rate-limiting.

After editing, `make monitoring-setup`.

## Cost model

- **Local:** $0. OpenObserve + Keep + MinIO all self-hosted.
- **Preview (M2)**: $0 incremental. OpenObserve joins the per-PR droplet; Parquet writes to DO Spaces (which Bootstrap TF already provisions — $5/mo for 250GB shared across all envs).
- **Production (M4)**: $0 incremental once the prod droplet exists. Same OpenObserve container, same Discord webhooks.

> No Datadog, no Grafana Cloud, no PagerDuty subscription. Discord's `@here` mechanism substitutes for paid paging on a solo-dev tier.

## What's still aspirational

- **`status.gatheringatthegrove.com` DNS** — production terraform mentions the hostname as a comment (line 162) but no DNS record + no reverse-proxy config. Lands with M2 droplet.
- **SSL cert expiry monitors** — the 4 placeholder monitors are in `monitors.json` but will fail locally because there are no real certs. They become meaningful when M2 droplet provisions Let's Encrypt.
- **Production kill-test** — the acceptance criterion `Killing Odoo in production triggers Slack + PagerDuty in <2 min` requires M2 droplet to exist. Local equivalent (this stack) is the closest verifier today.

## Related

- `docs/RUNBOOKS.md` — one entry per alert
- `openobserve/monitors.json` — synthetic monitor source-of-truth
- `openobserve/alerts.json` — alert rule source-of-truth
- `keep/providers.yml` — Discord webhook definitions
- `keep/workflows.yml` — severity → channel routing
- `scripts/setup-monitoring.py` — bootstrap script (idempotent)
- `scripts/smoke-test-monitoring.sh` — GATH-44 acceptance verifier
