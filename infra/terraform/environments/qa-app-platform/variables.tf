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
  description = "Subdomain under the apex zone. Final FQDN is <qa_subdomain>.<cloudflare_zone_name>. Distinct from the monolith QA's `qa` subdomain during the parallel-cutover window — uses `qa-l3` so both envs serve simultaneously without DNS conflict. The eventual cutover (ADR-007 Phase 4) flips the existing `qa` records over and this env's subdomain returns to plain `qa`."
  type        = string
  default     = "qa-l3"
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

# === ACME endpoint (Caddy / Let's Encrypt) ===

variable "acme_endpoint" {
  description = "ACME directory URL Caddy uses for cert issuance. Default = LE PROD. Set to LE STAGING when iterating heavily; matches the monolith QA env's pattern."
  type        = string
  default     = "https://acme-v02.api.letsencrypt.org/directory"
  validation {
    condition     = contains(["https://acme-v02.api.letsencrypt.org/directory", "https://acme-staging-v02.api.letsencrypt.org/directory"], var.acme_endpoint)
    error_message = "acme_endpoint must be LE prod or staging URL exactly."
  }
}
