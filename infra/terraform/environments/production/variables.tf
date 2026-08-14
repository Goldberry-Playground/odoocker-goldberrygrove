variable "do_token" {
  description = "DigitalOcean API token (deploy-scoped). Injected as TF_VAR_do_token via op run."
  type        = string
  sensitive   = true
}

variable "cloudflare_api_token" {
  description = "Cloudflare ACCOUNT-scoped API token covering all four brand zones: Zone.DNS edit + Zone.Zone read + Zone Settings edit + SSL and Certificates edit (the latter authorizes cloudflare_origin_ca_certificate; the legacy Origin CA Key is deprecated). 1P field: account_cloudflare_api_token."
  type        = string
  sensitive   = true
}

variable "spaces_access_id" {
  description = "DO Spaces access key (plumbing key, All Buckets) for the digitalocean provider's S3-protocol bucket operations AND the droplet's rclone backup uploads."
  type        = string
  sensitive   = true
}

variable "spaces_secret_key" {
  description = "DO Spaces secret key paired with spaces_access_id."
  type        = string
  sensitive   = true
}

variable "admin_ip_cidr" {
  description = "Operator IPv4 CIDR for the blogs/Odoo SSH allowlist. Use `curl -4 ifconfig.me`/32. Default matches the value live in prod state (blogs firewall port-22 rule) and the qa-app-platform default -- keeping it here rather than in a gitignored tfvars is what makes the SSH rule reproducible from code (GOL-385)."
  type        = string
  default     = "74.47.41.38/32"
  validation {
    condition     = can(regex("^[0-9.]+/[0-9]+$", var.admin_ip_cidr))
    error_message = "admin_ip_cidr must be a valid IPv4 CIDR like 74.47.41.38/32"
  }
}

variable "healthchecks_ping_url" {
  description = "Healthchecks.io ping URL for the nightly blogs backup dead-man's switch. Empty string disables pings. Feeds the blogs droplet's user_data, so a placeholder here does not merely misconfigure the backup ping -- it changes the user_data hash and forces a droplet REPLACE (GOL-385)."
  type        = string
  default     = ""
  # A placeholder in user_data is invisible until it detonates: the plan just
  # says "must be replaced" with a (sensitive value) diff and no hint why.
  # Fail loudly at plan time instead.
  validation {
    condition     = !can(regex("REPLACE-UUID", var.healthchecks_ping_url))
    error_message = "healthchecks_ping_url still holds the REPLACE-UUID placeholder. Set the real ping URL (1P: Goldberry Grove - Admin/Grove Infra/healthchecks_ping_url) or \"\" to disable pings -- a placeholder forces a blogs droplet replace."
  }
}

variable "odoo_backup_healthchecks_ping_url" {
  description = "Healthchecks.io ping URL for the nightly Odoo FILESTORE backup dead-man's switch (GOL-99). Deliberately a SEPARATE check from var.healthchecks_ping_url: one check per job, else a green blogs ping masks a dead Odoo backup. The script pings only on success, so a silent failure trips the check. Empty string disables pings - acceptable for `plan`, but an unmonitored backup is not a backup: populate this before the prod apply (GOL-382)."
  type        = string
  default     = ""
}

variable "region" {
  description = "DO region for all production resources."
  type        = string
  default     = "nyc3"
}

variable "blogs_droplet_size" {
  description = "Blogs droplet size. 4x Ghost (~150MB each) + MySQL (~400MB) + Caddy fits in 2GB with headroom."
  type        = string
  default     = "s-2vcpu-2gb"
}

variable "droplet_image" {
  description = "Base image for droplets."
  type        = string
  default     = "ubuntu-24-04-x64"
}

variable "ghost_tag" {
  description = "Ghost image tag. Pin to a specific 6.x digest after first apply (Renovate bumps it)."
  type        = string
  default     = "6-alpine"
}

variable "mysql_tag" {
  description = "MySQL image tag (Ghost 6 requires MySQL 8)."
  type        = string
  default     = "8.4"
}

variable "caddy_tag" {
  description = "Official Caddy image tag. No DO-DNS plugin needed - TLS uses CF Origin CA cert files, not ACME. Shared by the blogs droplet (blogs.tf) and the Odoo droplet (odoo.tf) - both terminate TLS with Origin CA cert files."
  type        = string
  default     = "2-alpine"
}

# === Track 2 (ADR-007 Phase 6, GOL-105) - Managed Postgres ==================

variable "pg_size" {
  description = "DO Managed Postgres size slug. Prod runs BASIC tier (db-s-1vcpu-2gb, ~$30/mo) for automatic daily backups + 7-day PITR, per ADR-007 D3/D6. QA L3 runs dev tier (db-s-1vcpu-1gb) - the size-up is the whole point of the prod spend envelope."
  type        = string
  default     = "db-s-1vcpu-2gb"
}

variable "pg_version" {
  description = "Postgres major version. Odoo 19 is tested through PG 17; match the QA L3 + odoocker pg image (POSTGRES_VERSION=17) so behavior is consistent across envs."
  type        = string
  default     = "17"
}

variable "pg_node_count" {
  description = "Managed PG node count. 1 = standalone (no HA). Per ADR-007 D6 an HA standby (+$30/mo) is deferred until traffic warrants it - flip to 2 to add one."
  type        = number
  default     = 1
}

# === Track 2 (ADR-007 Phase 6, GOL-105) - Odoo droplet ======================

variable "odoo_droplet_size" {
  description = "Odoo droplet size. Prod runs s-2vcpu-4gb (~$24/mo per ADR-007 D6) - double the QA L3 s-1vcpu-2gb so Odoo 19 + workers have headroom under real market-season load. Only stateful compute Level 3 keeps on a droplet (Postgres -> Managed PG, frontends -> App Platform)."
  type        = string
  default     = "s-2vcpu-4gb"
}

variable "odoo_filestore_volume_size_gb" {
  description = "Size (GiB) of the durable block volume backing the Odoo filestore (/var/lib/odoo): every product photo + all ir.attachment binaries. Must survive a droplet replace (GOL-93). Sized up from QA L3's 10 GiB. This is the resource GOL-99 wires its nightly backup into."
  type        = number
  default     = 50

  validation {
    condition     = var.odoo_filestore_volume_size_gb >= 1
    error_message = "odoo_filestore_volume_size_gb must be at least 1 (DO block-volume minimum)."
  }
}

variable "odoo_image_tag" {
  description = "Tag of the grove-odoo image to deploy (ghcr.io/goldberry-playground/grove-odoo:<tag>). 'latest' tracks main; pin to a SHA for a reproducible prod release."
  type        = string
  default     = "latest"
}

variable "custom_modules_ref" {
  description = "grove-odoo-modules git ref the prod custom-modules-sync (git-sync) sidecar checks out into /workspace/current. MUST be a pinned 40-char commit SHA -- prod NEVER tracks a moving branch (a merge to grove-odoo-modules main would otherwise auto-deploy to prod within GITSYNC_PERIOD with no review gate). Same reproducible-release rationale as var.odoo_image_tag. Bumping this is a reviewed infra PR (see the 'Custom modules' section of the repo README). GOL-484 checkout go-live (board-approved 2026-08-07, interaction 5b2c6abb): bumped to 8accb94 -- the first checkout-capable grove_headless HEAD (checkout d565b58 + 3-tenant Stripe webhook verify ff8eefb/GOL-1020 + per-unit deposit split 6f62dd5/GOL-1036 + destination WV tax c44e72c/GOL-1021 + itemized line_items 56dcdf4/GOL-1057). Confirmed exposes /grove/api/v1/products|cart|shipping|stripe/webhook, which the prior pin f8ef75d1 (and the live root-compose pin 515dcb3f) predate -- live prod 404s on those routes today (GOL-484 recon). Prod grove_headless is UNINSTALLED (GOL-1258 prod-DB check), so on the go-live rebuild the entrypoint's revision-advance path runs --init=base,grove_headless (a FRESH install, not an upgrade -- GOL-1214 sign-off); no migration, no data-loss risk (bare managed PG, only default company id 1)."
  type        = string
  default     = "8accb943d664a78df69f79eab689e7024d2b2445"

  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.custom_modules_ref))
    error_message = "custom_modules_ref must be a full 40-char lowercase hex commit SHA -- branch names like 'main'/'HEAD' are rejected so prod can never track a moving ref (GOL-892)."
  }
}

# === Track 2 (ADR-007 Phase 6, GOL-105/GOL-116) - App Platform frontends =====

variable "app_instance_size_slug" {
  description = "App Platform instance size for all four frontends. Prod runs the PROFESSIONAL (dedicated-CPU) tier apps-d-1vcpu-0.5gb (~$12/mo each => ~$48/mo for 4, ADR-007 D6) vs QA L3's basic apps-s-1vcpu-0.5gb (~$5/mo). The pro tier buys dedicated CPU + zero-downtime deploys under real market-season load. Free-form string passed to the DO API; validate against `doctl apps tier instance-size list` before changing."
  type        = string
  default     = "apps-d-1vcpu-0.5gb"
}

variable "hub_image_tag" {
  description = "Tag of the grove-hub image on GHCR (ghcr.io/goldberry-playground/grove-hub:<tag>) that App Platform pulls. 'latest' tracks grove-sites CI; pin to a SHA to lock a reproducible prod release. Same pattern as var.odoo_image_tag."
  type        = string
  default     = "latest"
}

variable "tenant_image_tag" {
  description = "Tag of the grove-goldberry / grove-ggg / grove-nursery images on GHCR that the tenant App Platform apps pull. One shared tag because grove-sites CI publishes all four images from the same commit -- pinning tenants to different tags would deploy skewed monorepo states."
  type        = string
  default     = "latest"
}

variable "grove_revalidate_secret" {
  description = "Signed-webhook secret for grove-sites' /api/revalidate endpoint (all four apps share it). Rotates whenever this TF applies with a new value; consumers (Odoo webhooks, Ghost webhooks) need re-seeding when it changes. GENERAL (not SECRET) scope on the app, same provider-drift reason as odoo_api_keys. >=32 chars; generate with `openssl rand -hex 32`. Read from 1Password via TF_VAR_grove_revalidate_secret -- no default so a bare apply cannot ship a placeholder secret to prod."
  type        = string
  sensitive   = true
  validation {
    condition     = length(var.grove_revalidate_secret) >= 32
    error_message = "grove_revalidate_secret must be at least 32 characters (use `openssl rand -hex 32`)."
  }
}

variable "odoo_api_keys" {
  description = "Per-tenant Odoo API keys (bearer auth for authenticated /grove/api/v1 endpoints, e.g. order creation). Global-scope res.users.apikeys records minted on the PROD Odoo -- Odoo 19 bearer auth requires scope NULL keys. GENERAL (not SECRET) scope on the app: DO returns SECRET envs encrypted, so the provider re-diffs them every plan (upstream provider issues #869/#514) and would fire the nightly drift alert forever. The value lives in TF state regardless of type; state stays in the Spaces backend, never in the repo. Keys are revocable in Odoo (Settings -> Users -> API Keys). Read from TF_VAR_odoo_api_keys; stub defaults keep `plan` working before the real keys are minted."
  type        = map(string)
  sensitive   = true
  default = {
    goldberry = "prod-stub-no-odoo-api-key-yet"
    ggg       = "prod-stub-no-odoo-api-key-yet"
    nursery   = "prod-stub-no-odoo-api-key-yet"
  }
  validation {
    condition     = alltrue([for t in ["goldberry", "ggg", "nursery"] : contains(keys(var.odoo_api_keys), t)])
    error_message = "odoo_api_keys must contain keys: goldberry, ggg, nursery."
  }
}

variable "ghost_content_keys" {
  description = "Per-frontend Ghost Content API keys for the live blog.* hosts on the Track-1 blogs droplet. grove-sites' requireEnv() throws on empty in production, so stub defaults keep `plan` working until the four Ghost instances are provisioned and their Content API keys minted (Ghost Admin -> Settings -> Integrations). Read from TF_VAR_ghost_content_keys once real. Content keys are read-only + rotatable in Ghost, so GENERAL scope is fine."
  type        = map(string)
  sensitive   = true
  default = {
    hub       = "prod-stub-no-ghost-key-yet"
    goldberry = "prod-stub-no-ghost-key-yet"
    ggg       = "prod-stub-no-ghost-key-yet"
    nursery   = "prod-stub-no-ghost-key-yet"
  }
  validation {
    condition     = alltrue([for t in ["hub", "goldberry", "ggg", "nursery"] : contains(keys(var.ghost_content_keys), t)])
    error_message = "ghost_content_keys must contain keys: hub, goldberry, ggg, nursery."
  }
}

variable "ghost_smtp_host" {
  description = "Mailgun SMTP relay host for Ghost transactional email (GOL-248). US region default; use smtp.eu.mailgun.org for an EU account."
  type        = string
  default     = "smtp.mailgun.org"
}

variable "ghost_smtp_port" {
  description = "Mailgun SMTP submission port. 587 = STARTTLS (mail__options__secure=false)."
  type        = string
  default     = "587"
}

variable "ghost_staff_device_verification" {
  description = "Ghost 6 staff-login device-verification (GOL-248). Kept false until Mailgun SMTP is populated + verified live, then flipped to \"true\" via TF_VAR_ghost_staff_device_verification so a broken transport can't 500 staff logins."
  type        = string
  default     = "false"
}

variable "ghost_smtp" {
  description = "Per-tenant Mailgun SMTP credentials for Ghost transactional email (GOL-248). Each tenant sends from a distinct mg.<domain> sending subdomain. Empty stub creds keep `plan` working until Mailgun is provisioned (GOL-248 API-key step); with empty user/pass the SMTP transport is inert and staffDeviceVerification stays false, so no regression pre-cutover. Read from TF_VAR_ghost_smtp (sourced from 1Password) at cutover."
  type = map(object({
    user = string
    pass = string
    from = string
  }))
  sensitive = true
  # `from` is a bare address (no display name): the droplet backup script
  # sources this .env in bash, so spaces/<> would break `set -euo pipefail`.
  # Ghost falls back to the publication title as the sender display name.
  default = {
    # hub sends from `send.`, NOT `mg.` — mg.gatheringatthegrove.com is
    # registered to a Mailgun account we do not control and carries live DNS
    # (see scripts note 9 in mailgun-domains.sh). Verified 2026-08-07: the
    # Mailgun account holds send.gatheringatthegrove.com (active) and has no
    # mg.gatheringatthegrove.com. Using `mg.` here silently fails to deliver.
    hub       = { user = "", pass = "", from = "noreply@send.gatheringatthegrove.com" }
    goldberry = { user = "", pass = "", from = "noreply@mg.goldberrygrove.farm" }
    ggg       = { user = "", pass = "", from = "noreply@mg.woodworkingeorge.com" }
    nursery   = { user = "", pass = "", from = "noreply@mg.atthegrovenursery.com" }
  }
  validation {
    condition     = alltrue([for t in ["hub", "goldberry", "ggg", "nursery"] : contains(keys(var.ghost_smtp), t)])
    error_message = "ghost_smtp must contain keys: hub, goldberry, ggg, nursery."
  }
}

# === Observability — platform plane (GOL-381) ===============================

variable "discord_webhook_url" {
  description = "Discord webhook for #grove-ops paging. observability.tf appends Discord's Slack-compat suffix (`/slack`) so DigitalOcean's Slack-shaped alert payload is accepted — DO has no native Discord target. Secret: never inline it; injected as TF_VAR_discord_webhook_url via `op run` (1P field: discord_webhook_url). Empty string is NOT valid: it would silently produce alerts that page nowhere."
  type        = string
  sensitive   = true

  validation {
    # A malformed/blank webhook does not fail the apply — DO accepts the alert
    # and simply never delivers. That is a silent monitoring outage, so it is
    # caught here at plan time instead.
    condition     = can(regex("^https://(discord\\.com|discordapp\\.com)/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+$", var.discord_webhook_url))
    error_message = "discord_webhook_url must be a bare Discord webhook URL (https://discord.com/api/webhooks/<id>/<token>) with NO trailing /slack — observability.tf appends that itself."
  }
}

variable "alert_emails" {
  description = "Email recipients for production platform-plane alerts. Kept as a second delivery path alongside Discord on every alert: email does not depend on the webhook being valid or on anyone having Discord open."
  type        = list(string)
  default     = ["joshua_dunbar@me.com"]

  validation {
    condition     = length(var.alert_emails) > 0
    error_message = "alert_emails must not be empty — Discord alone is a single delivery path."
  }
}

variable "alert_discord_channel" {
  description = "Human-readable channel label carried in DO's Slack payload. Inert for delivery (Discord routes by webhook, not by this field) but the provider requires it; it shows up in the alert body, so it should name where the page is expected to land."
  type        = string
  default     = "#grove-ops"
}

variable "uptime_check_targets" {
  description = "Public URLs probed by DO's global uptime network, keyed by a short name used in the check/alert names. Defaults cover ONLY the hosts verified serving HTTP 200 on 2026-07-15; blog.gatheringatthegrove.com + blog.goldberrygrove.farm are deliberately excluded while they return 404, because a permanently-red alert trains responders to ignore the channel. Add them here once they serve."
  type        = map(string)
  default = {
    blog-ggg     = "https://blog.woodworkingeorge.com/"
    blog-nursery = "https://blog.atthegrovenursery.com/"
  }
}

variable "blog_apex_redirects_enabled" {
  description = "Enable the narrow apex/content/* → blog.<apex>/content/* 301 asset redirect (redirects.tf). Default false: blog.* vhosts 404 until the blogs-droplet url-flip apply completes, and the rule must be OFF until each apex is cut over. Flip with -var=blog_apex_redirects_enabled=true in the cutover window AFTER blog.* verifies healthy (see docs/RUNBOOK-apex-launch-cutover.md Step 3). This is a NARROW /content/* rule only — the apex root and storefront routes must reach the App Platform origin, so it must never match `/`."
  type        = bool
  default     = false
}

variable "apex_cutover_live_keys" {
  description = "Per-apex switch for the App Platform Host-override Origin Rule (apex-cutover.tf). Each tenant key (hub/goldberry/ggg/nursery) listed here gets its http_request_origin Host-rewrite rule CREATED. Default [] (empty) ⇒ no rule, no CF API call ⇒ merge is inert. This is deliberately per-apex, NOT a single bool: the rule fires for every request to <apex> regardless of DNS, so enabling it for an apex still pointed at Ghost breaks that apex. Add a key ONLY in lockstep with flipping that apex's DNS to the proxied CNAME (canary order for GOL-1279/GOL-1390: [\"ggg\"] validation apex → then hub/goldberry/nursery). CONVERGENCE INVARIANT (finding #1, review 2026-08-12): this committed default is the SOURCE OF TRUTH and MUST equal the set of apexes currently live on the rule. Advance it (in a committed PR) immediately after each apex verifies — do NOT rely on the in-window `-var` override alone. prod-plan-guard.yml and every var-less `terraform apply` plan against THIS default; if it lags reality they plan DESTRUCTION of the live rule → flipped apex 403s and the guard goes permanently red. Requires the CF token to carry Origin/Config Rules Edit on the brand zones (see apex-cutover.tf token-scope note). See docs/RUNBOOK-apex-launch-cutover.md."
  type        = set(string)
  default     = ["ggg", "hub"] # GOL-1279/GOL-1390: ggg (2026-08-14) + hub/gatheringatthegrove.com (2026-08-14, verified 200 + x-do-orig-status:200 + app title) live on App Platform. goldberry still on Ghost; add its key when cut over. (nursery apex is ALSO live but was registered out-of-band — reconcile into state separately, see GOL-1279 follow-up, before adding its key here.)

  validation {
    condition     = alltrue([for k in var.apex_cutover_live_keys : contains(["hub", "goldberry", "ggg", "nursery"], k)])
    error_message = "apex_cutover_live_keys may only contain tenant keys: hub, goldberry, ggg, nursery."
  }
}

# --- Grove assets (ADR-009 amendment 2026-08-02, GOL-1122) -------------------
# ADR-009 states the hub "already holds" these in its deploy env. It did not:
# verified 2026-08-03 via `doctl apps spec get d5fa7795-...` that none of the
# three were on grove-hub-prod, so /api/assets/optimize was returning 503
# not_configured to every caller. These three variables are what put them there.
# GENERAL (not SECRET) app scope for the same provider re-diff reason documented
# on odoo_api_keys. No defaults, per grove_revalidate_secret's precedent: a bare
# apply must not be able to ship a placeholder credential to prod.

variable "grove_assets_key" {
  description = "Spaces access key ID for the grove-assets bucket, read by packages/assets spacesConfigFromEnv() on the hub's /api/assets/optimize upload path (and the social/ re-host seam per ADR-009). Minted by environments/assets as digitalocean_spaces_key.assets_rw and copied into 1Password by hand. Read from TF_VAR_grove_assets_key -- note the 1P field is named grove_assets_access_key_id, not grove_assets_key."
  type        = string
  sensitive   = true
}

variable "grove_assets_secret" {
  description = "Spaces secret key paired with grove_assets_key (same digitalocean_spaces_key.assets_rw). Read from TF_VAR_grove_assets_secret -- 1P field is grove_assets_secret_key."
  type        = string
  sensitive   = true
}

variable "grove_assets_optimize_token" {
  description = "Shared bearer token the discord-bridge presents to the hub's /api/assets/optimize. apps/hub/lib/assets/service.ts checkAuth() returns 503 not_configured when this is empty rather than failing loudly -- which is exactly how the unset prod value went unnoticed. The SAME value must also be set on the grove-discord-bridge App Platform app (DO UI; its committed spec is all REPLACE_ME placeholders, so never `doctl apps update --spec` that file)."
  type        = string
  sensitive   = true
}

# --- Prod checkout Stripe keys (GOL-973) -------------------------------------
# DEDICATED backend key, NOT a storefront key. Runbook §4 (docs/RUNBOOK-
# checkout-stripe-guardrails.md): QA shares one stripe-nursery-qa key between
# the storefront and grove_headless as an accepted QA-only shortcut; prod must
# not repeat it, or revoking a storefront key would also kill checkout (a
# self-inflicted revenue outage). These resolve from their OWN 1Password item
# (see .env.op), wired via TF_VAR_stripe_test_secret_key /
# TF_VAR_stripe_test_webhook_secret. Empty default keeps plan/apply working and
# checkout INERT until CFO mints the live keys AND the board greenlights a
# prod-checkout rebuild (activation replaces the droplet — board-gated per
# GOL-920). Sensitive so the value never prints in plan/apply output.
variable "stripe_test_secret_key" {
  description = "Stripe LIVE-mode restricted secret key (rk_live_...) for grove_headless prod checkout — scoped to Checkout Session create + webhook ops only (live equivalent of the QA rk_test_ scope GOL-956 proved sufficient). DEDICATED backend key, never a storefront key (runbook §4). Injected into /etc/grove/.env as lowercase `stripe_test_secret_key`; grove_headless reads it via os.environ. VALUE is its own item in the `Goldberry Grove - Admin` vault (minted by CFO under GOL-973); read via TF_VAR_stripe_test_secret_key. Empty default keeps apply/plan working and checkout inert until provisioned + board-greenlit."
  type        = string
  sensitive   = true
  default     = ""
}

variable "stripe_test_webhook_secret" {
  description = "Stripe LIVE-mode webhook signing secret (whsec_...) for grove_headless prod webhook verification, from the prod Stripe webhook endpoint registered under GOL-973. Its OWN 1Password field, not shared with any storefront. Injected into /etc/grove/.env as lowercase `stripe_test_webhook_secret`; grove_headless reads it via os.environ. Empty default keeps apply/plan working and webhook verification inert until provisioned + board-greenlit."
  type        = string
  sensitive   = true
  default     = ""
}

variable "shippo_api_key" {
  description = "Shippo LIVE API key for prod label purchase + rate quotes (grove_headless models/sale_order.py reads os.environ SHIPPO_API_KEY; UserError when empty, so the empty default keeps plan/apply working and fulfillment inert). 1P: op://Grove Prod/Shippo Prod Key/password. Feeds cloud-init user_data => activating requires a droplet REPLACE — rides the board-approved GOL-484 go-live rebuild, not a standalone apply."
  type        = string
  sensitive   = true
  default     = ""
}

variable "grove_shippo_webhook_token" {
  description = "Shared token authenticating Shippo's inbound tracking webhooks in prod (controllers/main.py compares os.environ GROVE_SHIPPO_WEBHOOK_TOKEN; empty => fail-closed). 1P: op://Grove Prod/Shippo Prod Key/webhook_token — DISTINCT from the QA token so revoking one stage never breaks the other. The same value must be embedded in the prod webhook URL registered with Shippo. user_data input — same replace semantics as shippo_api_key."
  type        = string
  sensitive   = true
  default     = ""
}
