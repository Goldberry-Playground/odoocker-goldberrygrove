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
6. **Discord bridge overlay + Cloudflare Tunnel** (`compose/docker-compose.discord.yml`,
   toggled by `discord_bridge_enabled`) — see below

## Discord bridge interactions endpoint + Cloudflare Tunnel (GOL-593 / GOL-598)

Codifies the hot-applied go-live so a **grove-obs rebuild brings the droplet-side
runtime (bridge container + tunnel connector) up from code alone**. The overlay runs the zero-dependency,
Ed25519-verified `apps/discord-bridge/server.ts` (via `node
--experimental-strip-types`) plus a **Cloudflare Tunnel** connector that exposes
it at `https://discord.gatheringatthegrove.com/interactions` with **no inbound
port and no origin cert** (Discord → CF edge → tunnel → `discord-bridge:8787`).
The firewall is untouched — the tunnel dials **out**.

What cloud-init lands when `discord_bridge_enabled = true` (default):
- `/etc/grove-obs/docker-compose.discord.yml` — digest-pinned `node` + `cloudflared`,
  both on the existing external `grove-obs_obs` network. Brought up **after** the
  base obs stack so that network exists.
- `/etc/grove-obs/discord-bridge.env` (0600) — `BUFFER_API_TOKEN`,
  `DISCORD_BOT_TOKEN`, `DISCORD_APP_ID`, `DISCORD_PUBLIC_KEY`,
  `DISCORD_WEEKLY_INSIGHTS_CHANNEL_ID`, `PORT`, rendered from 1P Grove Infra.
- `/etc/grove-obs/cloudflared.env` (0600) — `TUNNEL_TOKEN` from
  1P `Grove Infra/discord_tunnel_token`.
- `/etc/grove-obs/discord-bridge-src/` — the app source, delivered as a
  `data.archive_file` zip and unpacked, then bind-mounted **read-only**.

### Prerequisites that are NOT in this IaC

"From code alone" covers the droplet side only. `cloudflared` runs in
**token mode** (`tunnel --no-autoupdate run`), so its configuration lives in the
Cloudflare dashboard, not this repo — there is no `cloudflare` provider in
`versions.tf`. A rebuild into a fresh CF account, or after the tunnel is deleted,
will apply cleanly, start both containers and fire `grove-obs-ready`, while
Discord interactions fail. These four must already exist (or be recreated by
hand):

1. The **Cloudflare Tunnel object** itself — `discord_tunnel_token` references a
   pre-existing tunnel; it does not create one.
2. Its **ingress rule**: `discord.gatheringatthegrove.com` → `http://discord-bridge:8787`.
3. The **DNS record** for `discord.gatheringatthegrove.com` (CNAME to the tunnel).
4. The **Interactions Endpoint URL** registered in the Discord developer portal.

Note the port in (2) is fixed at `8787` dashboard-side. `discord_bridge_port`
only changes what the container listens on — raising it without editing the CF
ingress silently breaks every interaction, so treat 8787 as effectively pinned.

### Vendored source & keeping it in sync
`compose/discord-bridge-src/` is a **snapshot** of grove-sites `apps/discord-bridge`
(as deployed at go-live). It is vendored here only so the obs env can rebuild
without a cross-repo checkout or a secret-bearing clone at provision time. Refresh
it whenever the bridge changes upstream:
```bash
./scripts/sync-discord-bridge-src.sh /path/to/grove-sites   # copies apps/discord-bridge → compose/discord-bridge-src
```
**Provenance (update this line on every sync):**

| Field | Value |
|---|---|
| Upstream | `Goldberry-Playground/grove-sites` → `apps/discord-bridge` |
| Synced at | `8c3b619` (2026-07-20) — runtime files verified byte-identical |
| Deliberately excluded | `lib/*.test.ts`, `node_modules`, `.env*` (not `.env.example`) |

There is **no CI check that this snapshot is current** — drift is caught only by
whoever remembers to re-sync. That is the real cost of vendoring: a
security-relevant upstream fix (e.g. to `lib/verify.ts`, the signature path) can
land in grove-sites and never reach a grove-obs rebuild. Until a drift job
exists, treat the SHA above as the contract and re-sync when the bridge changes.

Note `compose/discord-bridge-src/tsconfig.json` does `extends: "../../tsconfig.json"`,
which resolves to `infra/terraform/tsconfig.json` — a path that does not exist in
this repo. So `npm run type-check` **cannot** run against the vendored copy; type
checking belongs upstream in grove-sites. This does not affect runtime: node
strips types at execution and never reads `tsconfig.json`.

The image digests in `compose/docker-compose.discord.yml` are pinned; bump them
deliberately (immutable infra). A future hardening is to build a digest-pinned
GHCR image for the bridge instead of a bind-mounted source tree (grove-sites CI,
Ada's domain) — tracked as a follow-up. That would also decouple grove-obs's
droplet lifetime from another repo's release cadence (see the replacement
warning under Apply), which is the strongest argument for doing it.

> **Adding the `archive` provider:** this env now also requires
> `hashicorp/archive`. Run `terraform init -upgrade -backend-config=backend.hcl`
> once to pull it into `.terraform.lock.hcl` before the first `plan`/`apply`.

## Apply (once validated live)

> ### ⚠️ Changing cloud-init REPLACES the droplet — it is not a converging apply
>
> `user_data` is `ForceNew: true` in the DigitalOcean provider, and this module
> sets `create_before_destroy = false`. So **any** edit to `cloud-init.yaml.tpl`
> or to anything interpolated into it (compose files, the vendored
> discord-bridge source, the discord toggle/secrets) makes `terraform apply`
> **destroy the running `grove-obs` droplet and build a new one** — it does not
> "converge" the live box. cloud-init only ever executes on first boot, so a
> user_data change can't be applied any other way.
>
> What a replacement costs here:
> - **New public IP** (no reserved IP on this env). Anything pointing at the obs
>   IP breaks until updated: the `rum_public_host` DNS record and any off-box
>   OTLP shipper allow-listed by IP.
> - **Keep's SQLite state is lost** — it lives in a local named volume, not on a
>   block volume (`volume_size_gb = 0`). OpenObserve's Parquet survives; it is in
>   Spaces.
> - Observability blind spot for the rebuild window.
>
> Because of that, codified-after-the-fact changes (like the GOL-598 discord
> overlay, which was hot-applied to the live box first) are **for the next
> rebuild**. Landing them in `main` is the point; applying them immediately is
> not. Before any apply here: run `terraform plan` and check explicitly for
> `must be replaced` / `forces replacement` on `module.obs_droplet`. If you see
> it and you did not intend a rebuild, stop.

```bash
cp backend.hcl.example backend.hcl        # git-ignored
cp terraform.tfvars.example terraform.tfvars   # or use op:// injection
terraform init -upgrade -backend-config=backend.hcl   # -upgrade: picks up hashicorp/archive
terraform plan      # review before first apply — CHECK FOR "forces replacement"
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
