# Grove Observability env (obs droplet)

Provisions the **separate observability plane** from the design spec (§4–6): a DO
droplet running **OpenObserve + Keep**, isolated from the app plane so monitoring
outlives an app outage. OpenObserve stores Parquet in **DO Spaces** (there's no
shared MinIO here — that's the app plane).

## Status — APPLIED 2026-07-11 (GOL-270)

First live apply is recorded. Board-approved separate prod obs droplet is up:

- Droplet `grove-obs` — `nyc3`, `s-2vcpu-4gb`. Current IP + URLs via
  `terraform output` (`obs_droplet_ip` / `openobserve_url` / `keep_url`) — the
  IP changes on any droplet replace, so it is not pinned here. (As of the first
  stand-up: `159.65.46.198`.)
- OpenObserve `:5080` · Keep `:3034` — both admin-only via the firewall; `:5080`
  also open to the agenticos droplet for cross-plane OTLP ingest. Verified live:
  `/healthz` 200 + root auth 200, all three containers up from unattended
  cloud-init.
- State: `s3://grove-tf-state/observability/terraform.tfstate` (DO Spaces, locked)
- Secrets flow 1Password `Grove Infra` → `TF_VAR_*` → cloud-init; nothing hardcoded.

Remaining before the rich alert layer produces signal: run `setup-monitoring.py`
(monitors/alerts/dashboards + Discord routing) and enable the agenticos collector
(GOL-54). See **Follow-ups**.

## What it manages
1. DO SSH key (CI, long-lived, obs-specific) + reference to the out-of-band admin key
2. Obs droplet (via the shared `modules/droplet`), no attached volume (Parquet →
   Spaces; Keep SQLite fits local disk)
3. DO Spaces bucket `grove-openobserve-data` (OpenObserve Parquet storage)
4. Firewall — SSH + Keep (3034) admin-only; OpenObserve (5080) admin **plus**
   `ingest_source_cidrs` (agenticos droplet `/32`) for cross-plane OTLP ingest
5. cloud-init — Docker + the standalone `compose/docker-compose.obs.yml`

## Apply (once validated live)
```bash
cp backend.hcl.example backend.hcl        # git-ignored
cp terraform.tfvars.example terraform.tfvars   # or use op:// injection
terraform init -backend-config=backend.hcl
terraform plan      # review before first apply
terraform apply
```

## Applying monitors/alerts/dashboards + Discord routing
Config-as-code is applied **against this droplet from CI/operator**, not from the
droplet, using `setup-monitoring.py`'s remote-URL support:
```bash
OPENOBSERVE_BASE_URL=http://<obs_ip>:5080 \
KEEP_BASE_URL=http://<obs_ip>:8080 \
OPENOBSERVE_ROOT_EMAIL=... OPENOBSERVE_ROOT_PASSWORD=... \
KEEP_WEBHOOK_TOKEN=... DISCORD_WEBHOOK_WARNING=... DISCORD_WEBHOOK_CRITICAL=... \
./scripts/setup-monitoring.py
```

## Follow-ups (need live iteration / other work)
- **Keep webhook/API published on `:8080` (GOL-279) — DONE.** `keep-backend` now
  publishes `8080:8080` (compose) and `grove-obs-fw` has an **admin-only** `8080`
  inbound rule (mirroring 5080/3034). Rationale: OpenObserve v0.91.1's SSRF guard
  rejects an alert destination whose URL resolves to a **private** IP at create
  time, so OO's `keep-webhook` destination must target the droplet's **public** IP
  (`terraform output keep_webhook_url`). That OO->Keep POST is a **host-local
  hairpin** (OpenObserve container -> host public IP:8080 -> Docker DNAT -> Keep)
  and never traverses the cloud firewall — verified 200/202 live. `setup-monitoring.py`
  reaches Keep internally (`keep-backend:8080` on the `obs` network), also not via
  this port; so the only external consumer is an admin, hence `admin_ip_cidr` only.
  Keep is additionally X-API-KEY (`WEBHOOK_TOKEN`) gated. Next: set
  `KEEP_EVENT_URL=<keep_webhook_url>/alerts/event?provider_id=openobserve` and re-run
  `setup-monitoring.py` so the OO destination + alerts load (GOL-278).
- **Cross-plane ingest:** the agenticos droplet (`159.223.171.231/32`) is now an
  allowed ingest source on `:5080` via `var.ingest_source_cidrs`. Still TODO: point
  the agenticos collector's `OPENOBSERVE_OTLP_BASE`/`_METRICS_URL` at
  `http://<obs_droplet_ip>:5080` and re-run Deploy Droplet (GOL-54). App-plane
  `synthetic-runner`/`cost-bridge` add their source `/32`s to the same var.
- **Cloudflare-WAF Bearer ingest endpoint** (spec §1) for the off-droplet GitHub
  Actions Playwright/Hurl crons.
- **`make` targets** (`obs-init/plan/apply/destroy`) + `op run` env-file, matching
  the other envs.
- **Release-manifest wiring** — the manifest (ADR-004) isn't implemented yet; pin
  OpenObserve/Keep tags there once it exists so the obs stack promotes qa→main
  with everything else.
