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
- **Keep API reachability for `setup-monitoring.py`:** `keep-backend` (API `:8080`)
  is only reachable inside the compose `obs` network — it is **not** published to
  the host nor opened in the firewall. A remote/CI `setup-monitoring.py` (which the
  droplet-provisioning notes point at `KEEP_BASE_URL=http://<obs_ip>:8080`) therefore
  can't reach it. On first live apply, either run `setup-monitoring.py` **from the
  droplet** (localhost) or publish `8080` to the host and add an admin-scoped
  `8080` firewall rule (mirroring 5080/3034). Deferred to the live pass because
  exposing Keep's API has a blast-radius/auth call to make (see `KEEP_AUTH_TYPE`).
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
