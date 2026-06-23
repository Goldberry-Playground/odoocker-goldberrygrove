# === Infisical provider auth (sensitive — injected via op run from 1Password) ===

variable "infisical_admin_client_id" {
  description = "Universal Auth Client ID for the tf-infisical-admin identity. From 1Password GoldberryGrove Infra / infisical_admin_client_id."
  type        = string
  sensitive   = true
}

variable "infisical_admin_client_secret" {
  description = "Universal Auth Client Secret for the tf-infisical-admin identity. From 1Password GoldberryGrove Infra / infisical_admin_client_secret."
  type        = string
  sensitive   = true
}

# === Layout (have defaults; override only if the org/project moves) ===

variable "infisical_host" {
  description = "Infisical API host. Use https://app.infisical.com (US), https://eu.infisical.com (EU), or your self-hosted URL."
  type        = string
  default     = "https://app.infisical.com"
}

variable "infisical_org_id" {
  description = "Goldberry Grove Infisical Cloud org UUID. Routing identifier (not a secret)."
  type        = string
  default     = "952236a8-4ed4-45c0-81e8-5157b48557a2"
}

variable "grove_odoocker_project_uuid" {
  description = "UUID of the grove-odoocker project in Infisical (the secret store the OIDC identities will read from)."
  type        = string
  default     = "850603f8-e175-4c38-9038-97a1e69d72e6"
}

variable "github_repo_full_name" {
  description = "Full owner/name of the GitHub repo whose workflows mint OIDC tokens. Used as a literal in the trust policy's bound_subject."
  type        = string
  default     = "Goldberry-Playground/odoocker-goldberrygrove"
}

variable "github_branch" {
  description = "Branch the workflows run from. Used as a literal in the trust policy's bound_subject + job_workflow_ref claim. Per memory/feedback_oidc_trust_policy_pattern.md — literals only, no globs."
  type        = string
  default     = "main"
}

# === The thing this env actually manages: per-workflow OIDC identities ===

# Map of workflow short-name → workflow filename. Source of truth for which
# workflows have OIDC identities in Infisical. Adding a workflow here on the
# next apply creates the identity, attaches OIDC auth bound to that file +
# ref, and grants project access.
#
# Two-tier identity model imposed by the Infisical free-tier 5-identity cap
# (decision recorded 2026-06-23 in [[feedback_oidc_trust_policy_pattern]]
# and the doc/ADR/003 follow-up):
#
#   - PROD-CREDENTIAL workflows get a strict per-workflow identity. bound_subject
#     pins repo+ref; bound_claims.job_workflow_ref pins the exact workflow file.
#     A compromised non-prod workflow CANNOT use this identity.
#
#   - LOW-RISK workflows share ONE "shared-readonly" identity. bound_subject
#     pins repo+ref; bound_claims.job_workflow_ref is OMITTED. Any workflow on
#     the configured repo+ref can use it. Justified by blast-radius analysis:
#     all sharing workflows already read the same secrets via their own
#     per-workflow identities, so sharing doesn't expand exposure.
#
# Pre-existing identities (manual + script-created during Phase 3-4 today)
# get deleted in Infisical UI before first apply of this env — see README.

variable "odoocker_prod_credential_workflows" {
  description = "Strict per-workflow identities for workflows that touch prod-credential paths (release.yml + any future prod-CD)."
  type        = map(string)
  default = {
    "release" = "release.yml"
  }
}

variable "odoocker_shared_readonly_workflows" {
  description = "Workflows allowed to use the shared-readonly identity. They all read the same grove-odoocker/prod secrets; bound_subject still pins repo+ref. This map is informational — the shared identity itself does NOT enforce per-workflow binding (that's the whole point), but listing here documents intent."
  type        = list(string)
  default = [
    "terraform-drift.yml",
    "sandbox-reaper.yml",
    "sandbox-deploy.yml",
  ]
}

variable "access_token_ttl_seconds" {
  description = "Lifetime of the access token Infisical issues after a successful OIDC exchange. Workflows just need long enough to fetch + use secrets in one run; 600s is generous for typical CI."
  type        = number
  default     = 600
}

variable "access_token_max_ttl_seconds" {
  description = "Hard cap on token refresh chain length. Even if the workflow somehow extended its lifetime, this is the ultimate cutoff."
  type        = number
  default     = 1800
}
