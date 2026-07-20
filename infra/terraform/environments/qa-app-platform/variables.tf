# === Provider credentials (sensitive — TF_VAR_* via op run) ===

variable "do_token" {
  description = "DigitalOcean API token. Scopes: droplet, domain, ssh-key, firewall, database, app. From GoldberryGrove Infra / do_token. ALSO passed to the Odoo droplet's Caddy container via DO_API_TOKEN env for DNS-01 ACME challenge under the qa zone (domain:write covers this -- no extra scope needed)."
  type        = string
  sensitive   = true
}

variable "cloudflare_api_token" {
  description = "Cloudflare API token scoped to gatheringatthegrove.com with Zone:DNS:Edit. From GoldberryGrove Infra / cloudflare_api_token."
  type        = string
  sensitive   = true
}

# === Operator inputs ===

variable "admin_ip_cidr" {
  description = "Operator IPv4 CIDR for SSH allowlist (Odoo droplet + Managed PG trusted-source). Use `curl -4 ifconfig.me`/32."
  type        = string
  default     = "74.47.41.38/32"
  validation {
    condition     = can(regex("^[0-9.]+/[0-9]+$", var.admin_ip_cidr))
    error_message = "admin_ip_cidr must be a valid IPv4 CIDR like 74.47.41.38/32"
  }
}

variable "ci_ssh_public_key" {
  description = "LONG-LIVED CI SSH public key for the Odoo droplet. Same key + same long-lived rationale as the monolith QA env (var.ci_ssh_public_key in environments/qa/variables.tf): stable string => TF state stable => no replace => deploy token doesn't need ssh_key:delete scope."
  type        = string
  default     = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINZChrLuSKoa9YVmXJ+Mnu599sypAjQRLTQy698R5gdR grove-qa-ci@long-lived-20260624"
}

# === Layout (have defaults; override only if migrating zones/regions) ===

variable "cloudflare_zone_name" {
  description = "Apex zone managed in Cloudflare that delegates the QA subdomain to DO. Must be active in Cloudflare."
  type        = string
  default     = "gatheringatthegrove.com"
}

variable "qa_subdomain" {
  description = "Subdomain under the apex zone. Final FQDN is <qa_subdomain>.<cloudflare_zone_name>. Was `qa-l3` during the ADR-007 parallel-cutover window; flipped to plain `qa` at the accelerated Phase 4 cutover (2026-07-04) once the monolith env released the qa zone. Changing this re-keys EVERYTHING: the delegated zone, NS records, App Platform domains, droplet DNS + Caddy vhosts (droplet replacements!) -- treat as a migration, not a knob."
  type        = string
  default     = "qa"
}

variable "region" {
  description = "DigitalOcean region. Must match the Managed PG region for private-network connectivity (DO Managed DB is regional, not multi-region) AND match grove-tf-state's region for backend latency."
  type        = string
  default     = "nyc3"
}

# === Odoo droplet ===

variable "odoo_droplet_size" {
  description = "Tiny droplet for Odoo only (no frontends, no Postgres). s-1vcpu-2gb is the smallest size that comfortably runs Odoo 19 + workers; cost ~$12/mo while running. The full QA monolith uses s-2vcpu-4gb because it also runs PG + 4 frontends — Level 3 offloads those, so this can drop to half the size."
  type        = string
  default     = "s-1vcpu-2gb"
}

variable "droplet_image" {
  description = "DigitalOcean droplet OS image slug. Ubuntu 24.04 LTS is the current default in this repo."
  type        = string
  default     = "ubuntu-24-04-x64"
}

variable "odoo_filestore_volume_size_gb" {
  description = "Size (GiB) of the block volume backing the Odoo filestore (/var/lib/odoo). Product photos + all ir.attachment binaries live here and must survive a droplet replace (GOL-93). QA default is modest (~$1/mo); Phase-6 prod copies this pattern and sizes it up. Minimum DO block volume is 1 GiB."
  type        = number
  default     = 10

  validation {
    condition     = var.odoo_filestore_volume_size_gb >= 1
    error_message = "odoo_filestore_volume_size_gb must be at least 1 (DO block-volume minimum)."
  }
}

# === Managed Postgres ===

variable "pg_size" {
  description = "DO Managed Database size slug for the Postgres cluster. db-s-1vcpu-1gb is the dev-tier minimum (~$15/mo). Per ADR-007 D6 budget envelope, QA gets dev-tier (no HA, no PITR) — if it crashes, redeploy. Prod replicates with a basic-tier (db-s-1vcpu-2gb, ~$30/mo, backups + PITR)."
  type        = string
  default     = "db-s-1vcpu-1gb"
}

variable "pg_version" {
  description = "Postgres major version. Odoo 19 requires PG 12+ and is tested through PG 17. Match the monolith QA's odoocker pg image (POSTGRES_VERSION in .env defaults to 17) so behavior is consistent across envs."
  type        = string
  default     = "17"
}

variable "pg_node_count" {
  description = "Managed PG node count. 1 = standalone (no HA). Per ADR-007 D6, QA stays standalone; prod can add a standby later when traffic warrants the $30/mo extra."
  type        = number
  default     = 1
}

# === Image tags for the Odoo droplet's compose stack ===

variable "odoo_image_tag" {
  description = "Tag of the grove-odoo image to deploy (ghcr.io/goldberry-playground/grove-odoo:<tag>). 'latest' tracks main; pin to a SHA for reproducibility."
  type        = string
  default     = "latest"
}

variable "caddy_image_tag" {
  description = "Tag of the grove-caddy image to deploy (ghcr.io/goldberry-playground/grove-caddy:<tag>). Same scheme as var.odoo_image_tag. Caddy in this env fronts ONLY Odoo (the 4 frontends move to App Platform), so the cert-rate-limit class that motivated PR-D's multi-issuer fallback shrinks to one identifier — much harder to trip."
  type        = string
  default     = "latest"
}

# === Observability droplet (Phase 1.5) ===

variable "obs_droplet_size" {
  description = "DigitalOcean droplet size for the observability droplet (OpenObserve + Keep + inline MinIO). s-1vcpu-2gb fits comfortably in QA per ADR-007 addendum. Cost ~$12/mo while running."
  type        = string
  default     = "s-1vcpu-2gb"
}

variable "openobserve_tag" {
  description = "OpenObserve image tag (public.ecr.aws/zinclabs/openobserve:<tag>). DIGEST-PINNED since 2026-07-04: upstream prunes old tags from public ECR (v0.17.2 vanished and every fresh obs droplet failed the pull; the old droplet had only survived on its local image cache). Tag-only pins on this registry are time bombs -- keep the @sha256 suffix on updates. Update docker-compose.monitoring.yml (local) in the same commit."
  type        = string
  default     = "v0.91.1@sha256:e1ff0445fab3e748ac4cf630308cc8493579e50d19ad255bb3a3b8c1b710aaf7"
}

variable "keep_tag" {
  description = "Keep (alert routing) image tag for both keep-api and keep-ui. Match docker-compose.monitoring.yml."
  type        = string
  default     = "latest"
}

# === ACME endpoint (Caddy / Let's Encrypt) ===

# === App Platform (Phase 2) ================================================

variable "hub_image_tag" {
  description = "Tag of the grove-hub image on GHCR (ghcr.io/goldberry-playground/grove-hub:<tag>) that App Platform pulls. 'latest' tracks grove-sites CI; pin to a SHA for reproducibility when locking a QA state for a debugging session. See infra/terraform/environments/qa/variables.tf for the same pattern in the monolith env."
  type        = string
  default     = "latest"
}

variable "tenant_image_tag" {
  description = "Tag of the grove-goldberry / grove-ggg / grove-nursery images on GHCR that the tenant App Platform apps pull. One shared tag because grove-sites CI publishes all four images from the same commit -- pinning tenants to different tags would deploy skewed monorepo states."
  type        = string
  default     = "latest"
}

variable "grove_revalidate_secret" {
  description = "Signed-webhook secret for grove-sites' /api/revalidate endpoint. Rotates whenever this TF applies with a new value; consumers (Odoo webhooks, Ghost webhooks) need to be re-seeded when it changes. Length must be >=32 chars; generate with `openssl rand -hex 32`. Read from GoldberryGrove Infra via TF_VAR_grove_revalidate_secret."
  type        = string
  sensitive   = true
  validation {
    condition     = length(var.grove_revalidate_secret) >= 32
    error_message = "grove_revalidate_secret must be at least 32 characters (use `openssl rand -hex 32`)."
  }
}

variable "odoo_api_keys" {
  description = "Per-tenant Odoo API keys (bearer auth for authenticated /grove/api/v1 endpoints, e.g. order creation). Global-scope res.users.apikeys records minted on the QA Odoo -- Odoo 19 bearer auth requires scope NULL keys. Read from 1Password (ODOO_API_KEYS_TF_JSON) via TF_VAR_odoo_api_keys; defaults keep the pre-key qa-stub behavior so plan still works without the secret."
  type        = map(string)
  sensitive   = true
  default = {
    goldberry = "qa-stub-no-odoo-api-key-yet"
    ggg       = "qa-stub-no-odoo-api-key-yet"
    nursery   = "qa-stub-no-odoo-api-key-yet"
  }
  validation {
    condition     = alltrue([for t in ["goldberry", "ggg", "nursery"] : contains(keys(var.odoo_api_keys), t)])
    error_message = "odoo_api_keys must contain keys: goldberry, ggg, nursery."
  }
}

# === ACME endpoint (Caddy / Let's Encrypt) ==================================

variable "acme_endpoint" {
  description = "ACME directory URL Caddy uses for cert issuance. Default = LE PROD. Set to LE STAGING when iterating heavily; matches the monolith QA env's pattern."
  type        = string
  default     = "https://acme-v02.api.letsencrypt.org/directory"
  validation {
    condition     = contains(["https://acme-v02.api.letsencrypt.org/directory", "https://acme-staging-v02.api.letsencrypt.org/directory"], var.acme_endpoint)
    error_message = "acme_endpoint must be LE prod or staging URL exactly."
  }
}

# === assets-ingest endpoint secrets (GOL-290 / GOL-293) =====================
# All four feed the hub app's env (apps.tf). Empty-string defaults keep
# `terraform plan` working without secrets (same philosophy as odoo_api_keys'
# qa-stubs); real values flow via TF_VAR_* from 1Password `Grove Infra` at
# apply time (`op run --env-file .env.op -- terraform apply`). An empty value
# makes the corresponding endpoint fail SAFE (503 not_configured), never open.

variable "grove_assets_access_key_id" {
  description = "DO Spaces access key id for the grove-assets bucket -- injected as GROVE_ASSETS_KEY, read by grove-sites' spacesConfigFromEnv(). Read from 1Password `Grove Infra`/grove_assets_access_key_id via TF_VAR_grove_assets_access_key_id."
  type        = string
  sensitive   = true
  default     = ""
}

variable "grove_assets_secret_key" {
  description = "DO Spaces secret key for the grove-assets bucket -- injected as GROVE_ASSETS_SECRET. Read from 1Password `Grove Infra`/grove_assets_secret_key via TF_VAR_grove_assets_secret_key."
  type        = string
  sensitive   = true
  default     = ""
}

variable "grove_assets_optimize_token" {
  description = "Shared bearer the discord-plugin presents to POST /api/assets/optimize -- injected as GROVE_ASSETS_OPTIMIZE_TOKEN. Minted per GOL-293; read from 1Password `Grove Infra`/grove_assets_optimize_token via TF_VAR_grove_assets_optimize_token."
  type        = string
  sensitive   = true
  default     = ""
}

variable "grove_brand_pr_token" {
  description = "GitHub token (contents:write + pull_requests:write on grove-sites) the brand-entry handler uses to open @grove/brand PRs -- injected as GROVE_BRAND_PR_TOKEN. Provision gated on a human GitHub account action (GOL-293); read from 1Password `Grove Infra`/grove_brand_pr_token via TF_VAR_grove_brand_pr_token once minted."
  type        = string
  sensitive   = true
  default     = ""
}

# === Stripe sandbox keys (EOM-July QA, per-tenant) ==========================
# Restricted (rk_test_) sandbox keys, one per storefront tenant, minted
# 2026-07-20 and stored in 1Password vault `Grove QA` as items
# stripe-{nursery,ggg,goldberry}-qa (field `secret_key`). Injected as
# STRIPE_SECRET_KEY on the tenant apps. qa-stub defaults keep `terraform
# plan` working without secrets (same philosophy as odoo_api_keys); the
# real values flow via TF_VAR_* from .env.op at plan/apply time.

variable "stripe_secret_key_goldberry" {
  description = "Stripe restricted sandbox secret key (rk_test_) for the goldberry storefront. From 1Password `Grove QA`/stripe-goldberry-qa/secret_key via TF_VAR_stripe_secret_key_goldberry."
  type        = string
  sensitive   = true
  default     = "qa-stub-no-stripe-key-yet"
}

variable "stripe_secret_key_ggg" {
  description = "Stripe restricted sandbox secret key (rk_test_) for the ggg storefront. From 1Password `Grove QA`/stripe-ggg-qa/secret_key via TF_VAR_stripe_secret_key_ggg."
  type        = string
  sensitive   = true
  default     = "qa-stub-no-stripe-key-yet"
}

variable "stripe_secret_key_nursery" {
  description = "Stripe restricted sandbox secret key (rk_test_) for the nursery storefront. From 1Password `Grove QA`/stripe-nursery-qa/secret_key via TF_VAR_stripe_secret_key_nursery."
  type        = string
  sensitive   = true
  default     = "qa-stub-no-stripe-key-yet"
}

# Webhook signing secrets do not exist yet (Stripe webhook endpoints land
# later this week). Empty default = wired but inert, same fail-safe pattern
# as grove_brand_pr_token: the env var is present with an empty value and
# webhook signature verification simply fails until the real whsec_ value
# is added to the 1Password items (field `webhook_secret`) and the
# corresponding .env.op lines are uncommented.

variable "stripe_webhook_secret_goldberry" {
  description = "Stripe webhook signing secret (whsec_) for the goldberry storefront. Not minted yet -- will live at 1Password `Grove QA`/stripe-goldberry-qa/webhook_secret; uncomment the .env.op line once the field exists (an op:// ref to a missing field is a hard `op run` failure)."
  type        = string
  sensitive   = true
  default     = ""
}

variable "stripe_webhook_secret_ggg" {
  description = "Stripe webhook signing secret (whsec_) for the ggg storefront. Not minted yet -- will live at 1Password `Grove QA`/stripe-ggg-qa/webhook_secret; uncomment the .env.op line once the field exists."
  type        = string
  sensitive   = true
  default     = ""
}

variable "stripe_webhook_secret_nursery" {
  description = "Stripe webhook signing secret (whsec_) for the nursery storefront. Not minted yet -- will live at 1Password `Grove QA`/stripe-nursery-qa/webhook_secret; uncomment the .env.op line once the field exists."
  type        = string
  sensitive   = true
  default     = ""
}

# === Ghost Content API keys (prod blogs droplet, read-only) =================
# The QA frontends read the PROD Ghost blogs (blog.<brand-zone>, the 4x
# Ghost 6 droplet in environments/production -- see docs/GHOST.md and
# blogs.tf). Content API keys are READ-ONLY by design (Ghost Content API
# is public-content-only), so pointing QA at prod Ghost cannot mutate
# content. Keys come from each Ghost admin -> Settings -> Integrations
# and live in 1Password `Grove Infra`. qa-stub defaults keep plan working
# until the .env.op refs are filled in.

variable "ghost_content_key_hub" {
  description = "Ghost Content API key for the hub journal (blog.gatheringatthegrove.com) -- injected as HUB_GHOST_CONTENT_API_KEY. From 1Password `Grove Infra` via TF_VAR_ghost_content_key_hub."
  type        = string
  sensitive   = true
  default     = "qa-stub-no-ghost-key-yet"
}

variable "ghost_content_key_goldberry" {
  description = "Ghost Content API key for blog.goldberrygrove.farm -- injected as GHOST_CONTENT_KEY on the goldberry app. From 1Password `Grove Infra` via TF_VAR_ghost_content_key_goldberry."
  type        = string
  sensitive   = true
  default     = "qa-stub-no-ghost-key-yet"
}

variable "ghost_content_key_ggg" {
  description = "Ghost Content API key for blog.woodworkingeorge.com -- injected as GHOST_CONTENT_KEY on the ggg app. From 1Password `Grove Infra` via TF_VAR_ghost_content_key_ggg."
  type        = string
  sensitive   = true
  default     = "qa-stub-no-ghost-key-yet"
}

variable "ghost_content_key_nursery" {
  description = "Ghost Content API key for blog.atthegrovenursery.com -- injected as GHOST_CONTENT_KEY on the nursery app. From 1Password `Grove Infra` via TF_VAR_ghost_content_key_nursery."
  type        = string
  sensitive   = true
  default     = "qa-stub-no-ghost-key-yet"
}
