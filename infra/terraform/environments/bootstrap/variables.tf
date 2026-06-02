# === Provider credentials (sensitive — set via TF_VAR_* env vars) ===

variable "do_token" {
  description = "DigitalOcean API token with droplet/domain/firewall/spaces/tag/account scopes (P1)."
  type        = string
  sensitive   = true
}

variable "cloudflare_api_token" {
  description = "Cloudflare API token with Zone:DNS:Edit on the grove zone."
  type        = string
  sensitive   = true
}

variable "github_token" {
  description = "GitHub PAT with `repo` scope (or fine-grained: Actions+Secrets Read/Write) on var.github_secrets_repo."
  type        = string
  sensitive   = true
}

# === Operator inputs (sensitive — set via terraform.tfvars or TF_VAR_*) ===

variable "admin_ip_cidr" {
  description = "Your home/office IP in CIDR form, e.g. 203.0.113.42/32. Used as a GH secret for the preview droplet firewall."
  type        = string
  validation {
    condition     = can(regex("^[0-9.]+/[0-9]+$", var.admin_ip_cidr))
    error_message = "admin_ip_cidr must be a valid CIDR like 203.0.113.42/32"
  }
}

variable "slack_ops_webhook" {
  description = "Slack incoming-webhook URL for the #ops channel (P8a). Failures from the sanitize cron post here."
  type        = string
  sensitive   = true
}

# === Layout (have defaults; override if needed) ===

variable "github_secrets_repo" {
  description = "Repo (owner/name) where the GH Actions secrets live."
  type        = string
  default     = "Goldberry-Playground/grove-sites"
}

variable "cloudflare_zone_name" {
  description = "Cloudflare zone that owns the apex domain."
  type        = string
  default     = "gatheringatthegrove.com"
}

variable "preview_subdomain" {
  description = "Subdomain under the zone where previews live. Final FQDN is <preview_subdomain>.<cloudflare_zone_name> (default: preview.gatheringatthegrove.com)."
  type        = string
  default     = "preview"
}

variable "region" {
  description = "DigitalOcean region for the Spaces bucket. Must match grove-tf-state region."
  type        = string
  default     = "nyc3"
}

variable "bucket_name" {
  description = "Spaces bucket that holds sanitized snapshots and filestore archives."
  type        = string
  default     = "grove-preview-data"
}

variable "lifecycle_expiration_days" {
  description = "Days before snapshots/ and filestore/ objects auto-expire from the bucket."
  type        = number
  default     = 7
}

variable "ssh_public_key_path" {
  description = "Path to the SSH public key uploaded to DO. Generated locally — do NOT generate via terraform (private key would leak into tfstate)."
  type        = string
  default     = "~/.ssh/grove-preview-deploy.pub"
}

variable "ssh_private_key_path" {
  description = "Path to the SSH PRIVATE key whose contents become the PREVIEW_SSH_PRIVATE_KEY GH secret."
  type        = string
  default     = "~/.ssh/grove-preview-deploy"
}
