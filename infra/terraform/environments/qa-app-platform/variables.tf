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

variable "custom_modules_ref" {
  description = "grove-odoo-modules git ref the custom-modules-sync (git-sync) sidecar checks out into /workspace/current. Written to /etc/grove/.env as CUSTOM_MODULES_REF and consumed by the compose's GITSYNC_REF=$${CUSTOM_MODULES_REF:-main}. QA INTENTIONALLY floats `main` by default (fast iteration) — unlike prod, which MUST pin a 40-char SHA (GOL-892). Set this to a full commit SHA to pin QA to a reviewed commit without editing the compose file, e.g. to freeze the store for a tester window so a mid-session merge to main can't shift behavior under testers. Accepts `main` or a 40-char lowercase hex SHA."
  type        = string
  default     = "main"

  validation {
    condition     = var.custom_modules_ref == "main" || can(regex("^[0-9a-f]{40}$", var.custom_modules_ref))
    error_message = "custom_modules_ref must be \"main\" (QA floating default) or a full 40-char lowercase hex commit SHA to pin."
  }
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
#
# These are the PER-TENANT App Platform storefront keys (#269); the singular
# stripe_test_* pair below (#270) is the Odoo backend key. Both coexist.

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

# === Publish-webhook HMAC secrets (GOL-985/986/1004) ========================
# Shared HMAC-SHA256 secret for the Odoo -> grove-sites "Publish Guide to
# Storefront" webhook. Odoo (the sender, on the Odoo QA droplet) signs the
# raw body with GROVE_PUBLISH_WEBHOOK_SECRET_<TENANT>; the grove-sites tenant
# app (the receiver) verifies with GROVE_PUBLISH_WEBHOOK_SECRET. The two MUST
# be byte-identical or every delivery 401s. Wire contract:
# grove-odoo-modules grove_headless/docs/publish-webhook-contract.md.
#
# Default "" => receiver fails CLOSED (401 on every delivery), sender skips.
# Same "wired-but-empty until provisioned" shape as the stripe_webhook_secret_*
# vars above. The value is provisioned by DevOps (Terra) in 1Password `Grove QA`
# (item grove-publish-webhook-<tenant>-qa, field `secret`); uncomment the
# matching .env.op ref once the item exists.
#
# NOTE (2026-07-30): goldberry was provisioned LIVE first (doctl app env +
# droplet .env) to unblock the GOL-1003 E2E; the 1Password item + .env.op ref
# is the durable follow-up (needs Grove QA vault WRITE, which the ops SA lacks).
# Do NOT `make qa-l3-up` before that item exists or the apply will zero the
# live goldberry secret (default "").
variable "grove_publish_webhook_secret_goldberry" {
  description = "HMAC secret for the goldberry publish webhook (Odoo sender <-> grove-sites receiver). From 1Password `Grove QA`/grove-publish-webhook-goldberry-qa/secret via TF_VAR_grove_publish_webhook_secret_goldberry. Generate with `openssl rand -hex 32`."
  type        = string
  sensitive   = true
  default     = ""
}

variable "grove_publish_webhook_secret_ggg" {
  description = "HMAC secret for the ggg publish webhook. Not provisioned yet; wired-but-empty (receiver 401s) until the 1Password `Grove QA`/grove-publish-webhook-ggg-qa/secret item + .env.op ref exist."
  type        = string
  sensitive   = true
  default     = ""
}

variable "grove_publish_webhook_secret_nursery" {
  description = "HMAC secret for the nursery publish webhook. Not provisioned yet; wired-but-empty (receiver 401s) until the 1Password `Grove QA`/grove-publish-webhook-nursery-qa/secret item + .env.op ref exist."
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
#
# GOL-1013: converged onto the SAME single JSON-map vault field prod reads
# (`ghost_content_keys_tf_json` -> TF_VAR_ghost_content_keys). One secret,
# one representation across both envs -- no silent drift on key rotation.
# Shape mirrors production/variables.tf verbatim.

variable "ghost_content_keys" {
  description = "Per-frontend Ghost Content API keys for the live blog.* hosts on the Track-1 blogs droplet (QA reads prod Ghost per the EOM-July decision; Content API keys are read-only). Read from the single JSON-map 1Password field `ghost_content_keys_tf_json` via TF_VAR_ghost_content_keys -- the same source prod reads, so the two envs can never drift. qa-stub defaults keep `plan` working until the .env.op ref resolves. Content keys are read-only + rotatable in Ghost, so GENERAL scope is fine."
  type        = map(string)
  sensitive   = true
  default = {
    hub       = "qa-stub-no-ghost-key-yet"
    goldberry = "qa-stub-no-ghost-key-yet"
    ggg       = "qa-stub-no-ghost-key-yet"
    nursery   = "qa-stub-no-ghost-key-yet"
  }
  validation {
    condition     = alltrue([for t in ["hub", "goldberry", "ggg", "nursery"] : contains(keys(var.ghost_content_keys), t)])
    error_message = "ghost_content_keys must contain keys: hub, goldberry, ggg, nursery."
  }
}

# === Stripe TEST-mode keys (sandbox checkout — GOL-688/696) =================
# grove_headless reads these from the odoo process environment via
# os.environ.get("stripe_test_secret_key") / os.environ.get("stripe_test_webhook_secret")
# (LOWERCASE names, controllers/main.py). The odoo entrypoint/odoorc.sh
# export every KEY=VALUE line in /etc/grove/.env into that environment, so
# these two vars flow straight through the cloud-init .env write_files block.
#
# RESOLVED 2026-07-30 (GOL-899): wired to the `stripe-nursery-qa` item in the
# `grove-qa` 1Password vault (see .env.op / RUNBOOK-checkout-stripe-guardrails).
# NOTE: an empty default here is NOT "zero regression" — it 503s the checkout
# SESSION route for ALL THREE storefronts, because the thin proxies (GOL-890)
# make this the only key that route reads. Keep the empty default so `plan`
# works without op, but treat an empty resolved value at APPLY time as an
# outage, not a safe no-op. The apply-time `op` account MUST be grove-devops-ro
# (reads grove-qa); the Admin-only SA silently resolves these to "".
variable "stripe_test_secret_key" {
  description = "Stripe TEST-mode secret key (sk_test_...) for grove_headless sandbox checkout. Injected into /etc/grove/.env as lowercase `stripe_test_secret_key`; grove_headless reads it via os.environ. VALUE is an item in the `grove-qa` 1Password vault (GOL-696); read via TF_VAR_stripe_test_secret_key once the CI/TF apply op account can read grove-qa. Empty default keeps apply/plan working and the checkout inert until provisioned."
  type        = string
  sensitive   = true
  default     = ""
}

variable "stripe_test_webhook_secret" {
  description = "Stripe TEST-mode webhook signing secret (whsec_...) for grove_headless webhook verification. Injected into /etc/grove/.env as lowercase `stripe_test_webhook_secret`; grove_headless reads it via os.environ. VALUE is an item in the `grove-qa` 1Password vault (GOL-696); read via TF_VAR_stripe_test_webhook_secret once the CI/TF apply op account can read grove-qa. Empty default keeps apply/plan working and webhook verification inert until provisioned."
  type        = string
  sensitive   = true
  default     = ""
}

# === Mailgun SMTP — transactional email (GOL-248/GOL-995) ==================
# Odoo reads these from /etc/grove/.env (SMTP_SERVER/PORT/SSL/USER/PASSWORD +
# EMAIL_FROM/FROM_FILTER); entrypoint.sh + odoorc.sh substitute them into the
# SMTP group of /etc/odoo/odoo.conf. Only smtp_password is a real secret — the
# server/port/ssl/user/from defaults are the hub Mailgun sending domain
# `send.gatheringatthegrove.com` per docs/RUNBOOK-mailgun-transactional-email.md.
#
# ⚠ NOT `mg.gatheringatthegrove.com` — that hostname has live DNS on our zone
# but belongs to an UNKNOWN/unmanaged Mailgun account (the Mailgun API returns
# 404 for it under our account). The hub was provisioned as `send.*`; the
# GOL-517 provisioning note in the RUNBOOK is authoritative. Do not "fix" these
# back to `mg.*`.
#
# Sending domain + credential are LIVE (GOL-995, verified 2026-08-18):
# `send.gatheringatthegrove.com` state=active with SPF+DKIM valid, and SMTP
# login `transactional@send.gatheringatthegrove.com` authenticates on
# smtp.mailgun.org:587 (STARTTLS). smtp_password VALUE lives in 1Password
# `Goldberry Grove - Admin` → `Mailgun | Goldberry Grove` → `smtp_password_hub`
# (op service account can read it at apply time). Empty default keeps
# plan/apply working and SMTP auth inert (zero regression) until injected via
# TF_VAR_smtp_password at the wiring apply.
variable "smtp_server" {
  description = "Mailgun SMTP relay host for Odoo transactional email. Injected into /etc/grove/.env as SMTP_SERVER. Default is the Mailgun US endpoint; switch to smtp.eu.mailgun.org for an EU account."
  type        = string
  default     = "smtp.mailgun.org"
}

variable "smtp_port" {
  description = "Mailgun SMTP port. 587 => Odoo negotiates STARTTLS (smtp_ssl=False). Injected as SMTP_PORT."
  type        = string
  default     = "587"
}

variable "smtp_ssl" {
  description = "Odoo smtp_ssl (SMTP_SSL). False on port 587 (STARTTLS, not implicit TLS)."
  type        = string
  default     = "False"
}

variable "smtp_user" {
  description = "Mailgun SMTP login for the hub sending domain. Injected as SMTP_USER. Default is the credential stored in 1Password (`Mailgun | Goldberry Grove` → smtp_login_hub) and validated live on smtp.mailgun.org:587."
  type        = string
  default     = "transactional@send.gatheringatthegrove.com"
}

variable "smtp_password" {
  description = "Mailgun SMTP password for smtp_user. Injected as SMTP_PASSWORD. VALUE is in 1Password `Goldberry Grove - Admin` → `Mailgun | Goldberry Grove` → smtp_password_hub; supply via TF_VAR_smtp_password at the wiring apply. Empty default keeps apply/plan working and SMTP auth inert (zero regression) until provisioned."
  type        = string
  sensitive   = true
  default     = ""
}

variable "email_from" {
  description = "Odoo email_from — display-name + address on the hub Mailgun sending domain. Injected as EMAIL_FROM (double-quoted in the .env because it is shell-`source`d and contains spaces + `<>`; odoorc.sh strips one quote layer for odoo.conf). Mailgun rejects a From outside the authenticated domain, so all QA order/shipping mail sends from the hub sending domain (per-brand From is a GOL-248 follow-up)."
  type        = string
  default     = "Gathering at the Grove <orders@send.gatheringatthegrove.com>"
}

variable "from_filter" {
  description = "Odoo from_filter (FROM_FILTER) — the authenticated Mailgun sending domain Odoo is allowed to send From."
  type        = string
  default     = "send.gatheringatthegrove.com"
}

variable "shippo_api_key" {
  description = "Shippo TEST API key for QA label purchase + rate quotes. grove_headless (models/sale_order.py) reads os.environ SHIPPO_API_KEY — raises UserError if unset, so the empty default keeps plan/apply working and label purchase inert until wired. 1P: op://Grove QA/Shippo Key/password (via TF_VAR_shippo_api_key in .env.op). Feeds cloud-init user_data — changing it REPLACES the QA odoo droplet."
  type        = string
  sensitive   = true
  default     = ""
}

variable "grove_shippo_webhook_token" {
  description = "Shared token authenticating Shippo's inbound tracking webhooks (grove_headless controllers/main.py reads os.environ GROVE_SHIPPO_WEBHOOK_TOKEN and compares). Empty default = webhook auth fails closed (no unauthenticated status updates). 1P: op://Grove QA/Shippo Key/webhook_token. The same value must be embedded in the webhook URL registered with Shippo. Feeds user_data — changing it REPLACES the QA odoo droplet."
  type        = string
  sensitive   = true
  default     = ""
}
