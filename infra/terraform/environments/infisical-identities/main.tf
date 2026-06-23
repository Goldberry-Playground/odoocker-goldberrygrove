###############################################################################
# OIDC identities in Infisical for odoocker workflows.
#
# Two-tier model (decision 2026-06-23, fits the free-tier 5-identity cap):
#
#   - PROD-CREDENTIAL workflows (var.odoocker_prod_credential_workflows) each
#     get a strict per-workflow identity. Trust policy pins repo+ref AND the
#     specific workflow_ref. A compromised non-prod workflow CANNOT use this
#     identity. Use for workflows that touch prod SSH keys, prod CD secrets.
#
#   - LOW-RISK workflows share ONE "shared-readonly" identity. Trust policy
#     pins repo+ref but OMITS the workflow_ref binding. Any workflow on the
#     configured repo+ref can use it. Used by terraform-drift, sandbox-reaper,
#     sandbox-deploy — all already read the same secrets via per-workflow
#     identities pre-compromise, so sharing doesn't expand blast radius.
#
# Outputs (see outputs.tf) expose each identity's UUID so the consuming
# workflow YAML files can reference them as literal env vars.
#
# Pre-existing identities created during Phase 3-4 (terraform-drift via UI,
# sandbox-reaper via script) get deleted in Infisical UI before first apply
# of this env — see README's "Operator setup" section.
###############################################################################

provider "infisical" {
  host = var.infisical_host
  auth = {
    universal = {
      client_id     = var.infisical_admin_client_id
      client_secret = var.infisical_admin_client_secret
    }
  }
}

# ── Tier 1: per-workflow identities for prod-credential workflows ───────────

resource "infisical_identity" "prod_workflow" {
  for_each = var.odoocker_prod_credential_workflows

  name   = "gh-oidc-odoocker-${each.key}"
  org_id = var.infisical_org_id

  # Org-level role is `no-access` — least privilege. Project membership
  # (below) is the only thing that grants any capability.
  role = "no-access"
}

resource "infisical_identity_oidc_auth" "prod_workflow" {
  for_each = var.odoocker_prod_credential_workflows

  identity_id = infisical_identity.prod_workflow[each.key].id

  oidc_discovery_url = "https://token.actions.githubusercontent.com"
  bound_issuer       = "https://token.actions.githubusercontent.com"

  # STRICT binding: workflow_ref pinned to this specific file. A compromised
  # different workflow on the same repo+ref CANNOT exchange tokens for this
  # identity. Per [[feedback_oidc_trust_policy_pattern]].
  bound_subject = "repo:${var.github_repo_full_name}:ref:refs/heads/${var.github_branch}"
  bound_claims = {
    job_workflow_ref = "${var.github_repo_full_name}/.github/workflows/${each.value}@refs/heads/${var.github_branch}"
  }

  access_token_ttl            = var.access_token_ttl_seconds
  access_token_max_ttl        = var.access_token_max_ttl_seconds
  access_token_num_uses_limit = 0
  access_token_trusted_ips    = [{ ip_address = "0.0.0.0/0" }]
}

resource "infisical_project_identity" "prod_workflow_viewer" {
  for_each = var.odoocker_prod_credential_workflows

  identity_id = infisical_identity.prod_workflow[each.key].id
  project_id  = var.grove_odoocker_project_uuid

  roles = [{ role_slug = "viewer" }]
}

# ── Tier 2: single shared-readonly identity for low-risk workflows ──────────

resource "infisical_identity" "shared_readonly" {
  name   = "gh-oidc-odoocker-shared-readonly"
  org_id = var.infisical_org_id
  role   = "no-access"
}

resource "infisical_identity_oidc_auth" "shared_readonly" {
  identity_id = infisical_identity.shared_readonly.id

  oidc_discovery_url = "https://token.actions.githubusercontent.com"
  bound_issuer       = "https://token.actions.githubusercontent.com"

  # LOOSE binding: only repo+ref. NO workflow_ref claim. Any workflow on
  # Goldberry-Playground/odoocker-goldberrygrove main can use this identity.
  # Acceptable because the consumers (terraform-drift, sandbox-reaper,
  # sandbox-deploy) all read the same grove-odoocker/prod secrets — sharing
  # doesn't expand blast radius vs the prior per-workflow design.
  #
  # Workflows allowed under this identity are declared (informationally only)
  # in var.odoocker_shared_readonly_workflows. The shared identity does NOT
  # enforce per-workflow binding — listing is for docs + future audit only.
  bound_subject = "repo:${var.github_repo_full_name}:ref:refs/heads/${var.github_branch}"
  bound_claims  = {}

  access_token_ttl            = var.access_token_ttl_seconds
  access_token_max_ttl        = var.access_token_max_ttl_seconds
  access_token_num_uses_limit = 0
  access_token_trusted_ips    = [{ ip_address = "0.0.0.0/0" }]
}

resource "infisical_project_identity" "shared_readonly_viewer" {
  identity_id = infisical_identity.shared_readonly.id
  project_id  = var.grove_odoocker_project_uuid

  roles = [{ role_slug = "viewer" }]
}
