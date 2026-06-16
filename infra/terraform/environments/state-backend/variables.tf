# === Provider credentials (sensitive — set via TF_VAR_* env vars) ===
# Recommended: source from 1Password via `op run --env-file=.env.op -- make ...`
# so the values never enter shell scrollback or this repo.

variable "do_token" {
  description = "DigitalOcean API token with spaces (read, update) + spaces_key (create, read, update, delete, create_credentials) scopes. Sourced from 1Password GoldberryGrove Infra/do_token."
  type        = string
  sensitive   = true
}

variable "github_token" {
  description = "GitHub PAT with Actions:Secrets Read+Write + Metadata:Read on var.github_secrets_repo (fine-grained), or classic with `repo` scope. Sourced from 1Password GoldberryGrove Infra/github_token."
  type        = string
  sensitive   = true
}

# === Layout (have defaults; override only if you need to) ===

variable "github_secrets_repo" {
  description = "Repo (owner/name) that receives SPACES_ACCESS_KEY_ID + SPACES_SECRET_ACCESS_KEY as GH Actions secrets."
  type        = string
  default     = "Goldberry-Playground/odoocker-goldberrygrove"
}

variable "region" {
  description = "DigitalOcean region for the state bucket. Should match the region your other envs deploy into."
  type        = string
  default     = "nyc3"
}

variable "bucket_name" {
  description = "Name of the Spaces bucket that holds Terraform remote state across all environments."
  type        = string
  default     = "grove-tf-state"
}
