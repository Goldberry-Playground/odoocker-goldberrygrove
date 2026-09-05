# === Provider credentials (sensitive — set via TF_VAR_* env vars) ===
# Recommended: source from 1Password via `op run --env-file=.env.op -- ...`
# so the values never enter shell scrollback or this repo.

variable "do_token" {
  description = "DigitalOcean API token. Sourced from 1Password Goldberry Grove - Admin/Grove Infra/do_token. Used by the DO provider for account-level auth."
  type        = string
  sensitive   = true
}

# The DO Terraform provider's bucket resources (digitalocean_spaces_bucket and
# friends) talk the S3 protocol, not the DO REST API — so they need a Spaces
# access key for provider auth, separate from the do_token. Same "plumbing"
# credential the state-backend env uses (All Buckets / Full Access). See the
# state-backend README section "Why two Spaces keys".
variable "spaces_bootstrap_access_key_id" {
  description = "Long-lived 'plumbing' Spaces access key ID used by the DO Terraform provider for bucket-level operations (create, versioning, lifecycle). Sourced from 1Password Goldberry Grove - Admin/Grove Infra/spaces_bootstrap_access_key_id."
  type        = string
  sensitive   = true
}

variable "spaces_bootstrap_secret_key" {
  description = "Companion secret to spaces_bootstrap_access_key_id. Same lifecycle, same source. Sourced from 1Password Goldberry Grove - Admin/Grove Infra/spaces_bootstrap_secret_key."
  type        = string
  sensitive   = true
}

# === Layout (have defaults; override only if migrating regions/renaming) ===

variable "region" {
  description = "DigitalOcean Spaces region. nyc3 matches the rest of Grove's infra (grove-tf-state, grove-assets)."
  type        = string
  default     = "nyc3"
}

variable "bucket_name" {
  description = "Spaces bucket name. Globally unique across DO Spaces. `grove-` prefix scopes it to this project. This bucket is the shared home for cutover archives (asana/, and future square/, venmo/ prefixes)."
  type        = string
  default     = "grove-cutover-archives"
}

variable "noncurrent_version_expiration_days" {
  description = "Days to retain NON-CURRENT object versions before expiry. Current versions are kept indefinitely (this is a rollback/audit surface). 365d is a generous recovery window for audit archives — longer than state's 90d because these are compliance/rollback artifacts, not tiny state blobs re-created every apply."
  type        = number
  default     = 365
}
