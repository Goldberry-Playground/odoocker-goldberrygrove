###############################################################################
# Per-workflow OIDC identities in Infisical for odoocker workflows.
#
# Pattern (one resource set per workflow, via for_each):
#   1. infisical_identity              — the identity at the org level
#   2. infisical_identity_oidc_auth    — OIDC trust policy bound to the
#                                        specific workflow file + branch
#                                        (literals only, no globs — per
#                                        memory/feedback_oidc_trust_policy_pattern.md)
#   3. infisical_project_identity      — grant Viewer role on grove-odoocker
#                                        (read-only — workflows fetch secrets,
#                                        never mutate them)
#
# Outputs (see outputs.tf) expose each identity's UUID so the consuming
# workflow YAML files can reference them as literal env vars.
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

# Base identities — one per workflow.
resource "infisical_identity" "odoocker_workflow" {
  for_each = var.odoocker_workflows

  name   = "gh-oidc-odoocker-${each.key}"
  org_id = var.infisical_org_id

  # Org-level role is `no-access` deliberately — these identities have no
  # org-wide privileges. The only thing they can do is what the project
  # membership (below) grants them: read secrets from grove-odoocker/prod.
  role = "no-access"
}

# OIDC auth method on each identity — the trust policy that decides which
# GitHub-signed JWTs are accepted as authentication.
resource "infisical_identity_oidc_auth" "odoocker_workflow" {
  for_each = var.odoocker_workflows

  identity_id = infisical_identity.odoocker_workflow[each.key].id

  oidc_discovery_url = "https://token.actions.githubusercontent.com"
  bound_issuer       = "https://token.actions.githubusercontent.com"

  # bound_subject and bound_claims together pin the trust policy to ONE
  # specific workflow file on ONE specific branch in ONE specific repo.
  # Per memory/feedback_oidc_trust_policy_pattern.md: literals only, no
  # globs. The repository+workflow_ref+ref triplet is what ties a token
  # to "this exact workflow."
  bound_subject = "repo:${var.github_repo_full_name}:ref:refs/heads/${var.github_branch}"
  bound_claims = {
    job_workflow_ref = "${var.github_repo_full_name}/.github/workflows/${each.value}@refs/heads/${var.github_branch}"
  }

  # bound_audiences intentionally empty — Infisical's secrets-action sets
  # the audience to a default that the API accepts when bound_audiences
  # is unset. Tighten later if Infisical changes that behavior.

  access_token_ttl            = var.access_token_ttl_seconds
  access_token_max_ttl        = var.access_token_max_ttl_seconds
  access_token_num_uses_limit = 0
  access_token_trusted_ips    = [{ ip_address = "0.0.0.0/0" }]
}

# Project membership — grants each identity Viewer (read-only) on the
# grove-odoocker project so it can fetch secrets from the `prod` env.
resource "infisical_project_identity" "odoocker_workflow_viewer" {
  for_each = var.odoocker_workflows

  identity_id = infisical_identity.odoocker_workflow[each.key].id
  project_id  = var.grove_odoocker_project_uuid

  roles = [
    {
      role_slug = "viewer"
    }
  ]
}
