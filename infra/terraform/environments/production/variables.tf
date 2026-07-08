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
  description = "Operator CIDR allowed SSH (e.g. 203.0.113.7/32)."
  type        = string
}

variable "healthchecks_ping_url" {
  description = "Healthchecks.io ping URL for the nightly blogs backup dead-man's switch. Empty string disables pings."
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
