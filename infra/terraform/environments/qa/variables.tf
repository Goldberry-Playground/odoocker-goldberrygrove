# === Provider credentials (sensitive — TF_VAR_* via op run) ===

variable "do_token" {
  description = "DigitalOcean API token. Scopes: droplet, domain, ssh-key, firewall. From GoldberryGrove Infra / do_token. ALSO passed through cloud-init to the Caddy container's DO_API_TOKEN env so Caddy can manage _acme-challenge TXT records under the delegated qa zone for DNS-01 wildcard TLS (domain:write covers this -- no extra scope needed)."
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
  description = "Operator IPv4 CIDR for SSH allowlist. Use `curl -4 ifconfig.me`/32."
  type        = string
  default     = "74.47.41.38/32"
  validation {
    condition     = can(regex("^[0-9.]+/[0-9]+$", var.admin_ip_cidr))
    error_message = "admin_ip_cidr must be a valid IPv4 CIDR like 74.47.41.38/32"
  }
}

variable "ci_ssh_public_key" {
  description = "LONG-LIVED CI SSH public key. Stable across workflow runs (was ephemeral before; see commit history). Public key hardcoded as default below; matching private key in 1Password 'GoldberryGrove Infra' / grove_qa_ci_ssh_private_key AND Infisical secret GROVE_QA_CI_SSH_PRIVATE_KEY (workflow fetches the private half from there). Stable string => TF state stable => no replace => no destroy scope needed on the deploy token."
  type        = string
  default     = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINZChrLuSKoa9YVmXJ+Mnu599sypAjQRLTQy698R5gdR grove-qa-ci@long-lived-20260624"
}

# NOTE: var.admin_ssh_public_key was removed when the qa_admin SSH key
# migrated from `resource` to `data` (DO best-practices alignment). The key
# is now created out-of-band (by Josh, once) and TF references it by name.
# To rotate: replace the DO SSH key named "grove-qa-admin" via DO UI / API,
# then re-apply TF (data source picks up the new fingerprint, droplet's
# ssh_keys list updates in place).

# === Layout (have defaults; override only if migrating zones/regions) ===

variable "cloudflare_zone_name" {
  description = "Apex zone managed in Cloudflare that delegates the QA subdomain to DO. Must be active in Cloudflare."
  type        = string
  default     = "gatheringatthegrove.com"
}

variable "qa_subdomain" {
  description = "Subdomain under the apex zone. Final FQDN is <qa_subdomain>.<cloudflare_zone_name> (default qa.gatheringatthegrove.com). All tenant URLs live under this delegated zone (qa.gatheringatthegrove.com itself = hub; qa-goldberry.<zone> = goldberry frontend; etc.)."
  type        = string
  default     = "qa"
}

variable "tenant_subdomains" {
  description = "Per-tenant subdomain prefixes under the qa zone. Each one gets a CNAME → the qa apex A record."
  type        = list(string)
  default     = ["goldberry", "ggg", "nursery", "odoo"]
  # qa.gatheringatthegrove.com itself = hub (apex A record handles it)
}

variable "region" {
  description = "DigitalOcean region for the QA droplet. Match grove-tf-state region for backend latency."
  type        = string
  default     = "nyc3"
}

variable "droplet_size" {
  description = "DigitalOcean droplet size slug. s-2vcpu-4gb is sufficient for Odoo + Postgres + 4 frontends + Caddy with headroom. Cost: ~$24/mo while running. Use s-4vcpu-8gb if Odoo struggles."
  type        = string
  default     = "s-2vcpu-4gb"
}

variable "droplet_image" {
  description = "DigitalOcean droplet OS image slug. Ubuntu 24.04 LTS is the current default in this repo."
  type        = string
  default     = "ubuntu-24-04-x64"
}

# === Image tags for the compose stack ===

variable "odoo_image_tag" {
  description = "Tag of the grove-odoo image to deploy (ghcr.io/goldberry-playground/grove-odoo:<tag>). 'latest' tracks main; pin to a SHA for reproducibility."
  type        = string
  default     = "latest"
}

variable "frontend_image_tags" {
  description = "Per-frontend image tags from ghcr.io/goldberry-playground/grove-<name>:<tag>. 'latest' tracks main."
  type        = map(string)
  default = {
    hub       = "latest"
    goldberry = "latest"
    ggg       = "latest"
    nursery   = "latest"
  }
}

# === ACME endpoint (Caddy / Let's Encrypt) ===

variable "acme_endpoint" {
  description = "ACME directory URL Caddy uses for cert issuance. Default = LE PROD (real browser-trusted certs). Set to LE STAGING (https://acme-staging-v02.api.letsencrypt.org/directory) when iterating heavily to avoid LE's 5/week-per-identifier-set rate limit. Staging has effectively unlimited budget (30k/3h) but produces certs with browser warnings (not browser-trusted root). qa-deploy.yml exposes this as workflow_dispatch input use_staging_acme."
  type        = string
  default     = "https://acme-v02.api.letsencrypt.org/directory"
  validation {
    condition     = contains(["https://acme-v02.api.letsencrypt.org/directory", "https://acme-staging-v02.api.letsencrypt.org/directory"], var.acme_endpoint)
    error_message = "acme_endpoint must be LE prod or staging URL exactly. Use prod for real certs, staging for rate-limit-free iteration."
  }
}

# === Optional: goldberry Ghost API key for /blog content ===

variable "ghost_key_goldberry" {
  description = "Content API key from the live blog.goldberrygrove.farm Ghost. Empty string disables /blog content fetch (frontend gracefully degrades). Get from Ghost Admin → Integrations → Add custom integration → copy Content API Key."
  type        = string
  sensitive   = true
  default     = ""
}
