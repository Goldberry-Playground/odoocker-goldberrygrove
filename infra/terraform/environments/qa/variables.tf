# === Provider credentials (sensitive — TF_VAR_* via op run) ===

variable "do_token" {
  description = "DigitalOcean API token. Scopes: droplet, domain, ssh-key, firewall. From GoldberryGrove Infra / do_token."
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

variable "ssh_public_key_path" {
  description = "Path to the SSH public key uploaded to DigitalOcean. Generated locally via `make qa-keygen` (which runs ssh-keygen if the file doesn't exist) — private key stays on operator's machine, public key uploaded here."
  type        = string
  default     = "~/.ssh/grove-qa-deploy.pub"
}

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

# === Optional: goldberry Ghost API key for /blog content ===

variable "ghost_key_goldberry" {
  description = "Content API key from the live blog.goldberrygrove.farm Ghost. Empty string disables /blog content fetch (frontend gracefully degrades). Get from Ghost Admin → Integrations → Add custom integration → copy Content API Key."
  type        = string
  sensitive   = true
  default     = ""
}
