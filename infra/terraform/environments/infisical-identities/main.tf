###############################################################################
# OIDC identities in Infisical for ALL Goldberry-Playground workflows.
#
# Designed to fit within Infisical's free-tier 5-identity cap (decision
# 2026-06-23 — see memory/feedback_oidc_trust_policy_pattern.md).
#
# Identity count at steady state: 5
#   1. tf-infisical-admin              — Universal Auth, manages all other identities (created by bootstrap script, not this env)
#   2. gh-oidc-odoocker-shared         — repo+ref binding only, no workflow_ref
#   3. gh-oidc-odoocker-release        — strict workflow_ref binding to release.yml
#   4. gh-oidc-grove-sites-shared      — same shape as #2 but for grove-sites
#   5. gh-oidc-grove-sites-release     — same shape as #3 but for grove-sites
#
# Security boundaries preserved:
#   ✓ Cross-repo isolation       (odoocker workflows can't use grove-sites identities)
#   ✓ Cross-tenant isolation     (different projects, different Viewer grants)
#   ✓ Prod-credential isolation  (release identities have strict workflow_ref pinning)
#
# Security boundary deliberately collapsed (accepted trade-off for the 5-cap):
#   ✗ Per-workflow isolation WITHIN a repo's low-risk workflows. Any workflow
#     on a repo's main branch can use that repo's shared identity. Mitigated
#     by code review + the workflow YAML pattern being well-documented.
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

# ── Projects (only those this env manages — grove-odoocker is pre-existing) ─

# grove-sites: created by this env. UUID computed at apply time; flows into
# the identity grants below via `local.repo_project_uuids`.
resource "infisical_project" "grove_sites" {
  name        = "grove-sites"
  slug        = "grove-sites"
  description = "Secrets for Goldberry-Playground/grove-sites workflows (ci, docker, preview-up/down, release). Provisioned by infra/terraform/environments/infisical-identities/."
  type        = "secret-manager"
}

# Resolve per-repo project UUIDs at one place — for grove-sites, the resource
# above provides it; for odoocker, the hardcoded value in var.repos["odoocker"]
# is used (the project pre-existed this env's creation).
locals {
  repo_project_uuids = {
    odoocker    = var.repos["odoocker"].project_uuid
    grove-sites = infisical_project.grove_sites.id
  }
}

# ── Tier 1: shared-readonly identity per repo ───────────────────────────────

resource "infisical_identity" "shared" {
  for_each = var.repos

  name   = "gh-oidc-${each.key}-shared"
  org_id = var.infisical_org_id
  role   = "no-access"
}

resource "infisical_identity_oidc_auth" "shared" {
  for_each = var.repos

  identity_id        = infisical_identity.shared[each.key].id
  oidc_discovery_url = "https://token.actions.githubusercontent.com"
  bound_issuer       = "https://token.actions.githubusercontent.com"

  # LOOSE binding: only `repository` claim, NO ref or workflow_ref pinning.
  # ANY workflow on the configured repo (any branch — main, qa, feature
  # branches) can authenticate as this identity. Required for Josh's dev
  # cycle: push to `qa` branch must work, push to `main` must also work,
  # both use this shared identity.
  #
  # Loosened from the original "only main branch" pattern (2026-06-24) to
  # enable qa-deploy.yml. Cross-repo isolation preserved — grove-sites
  # workflows still can't use this identity because their `repository`
  # claim doesn't match.
  #
  # `bound_subject` deliberately empty — relying on bound_claims.repository
  # alone. Infisical accepts empty subject + claims-only binding.
  bound_subject = ""
  bound_claims = {
    repository = each.value.github_repo_full_name
  }

  access_token_ttl            = var.access_token_ttl_seconds
  access_token_max_ttl        = var.access_token_max_ttl_seconds
  access_token_num_uses_limit = 0
  access_token_trusted_ips    = [{ ip_address = "0.0.0.0/0" }]
}

resource "infisical_project_identity" "shared_viewer" {
  for_each = var.repos

  identity_id = infisical_identity.shared[each.key].id
  project_id  = local.repo_project_uuids[each.key]

  roles = [{ role_slug = "viewer" }]
}

# ── Tier 2: strict per-workflow identities for prod-credential workflows ────

# Flatten the nested map (repo → prod_credential_workflows) into a single
# map keyed by "<repo>--<workflow>" so for_each can iterate it cleanly.
locals {
  prod_workflows_flat = merge([
    for repo_key, repo in var.repos : {
      for wf_name, wf_file in repo.prod_credential_workflows :
      "${repo_key}--${wf_name}" => {
        repo_key  = repo_key
        wf_name   = wf_name
        wf_file   = wf_file
        repo_full = repo.github_repo_full_name
        branch    = repo.github_branch
      }
    }
  ]...)
}

resource "infisical_identity" "prod" {
  for_each = local.prod_workflows_flat

  name   = "gh-oidc-${each.value.repo_key}-${each.value.wf_name}"
  org_id = var.infisical_org_id
  role   = "no-access"
}

resource "infisical_identity_oidc_auth" "prod" {
  for_each = local.prod_workflows_flat

  identity_id        = infisical_identity.prod[each.key].id
  oidc_discovery_url = "https://token.actions.githubusercontent.com"
  bound_issuer       = "https://token.actions.githubusercontent.com"

  # STRICT binding: pins repo + ref AND workflow_ref to a specific file. A
  # compromised workflow that isn't this exact file CANNOT use this identity.
  bound_subject = "repo:${each.value.repo_full}:ref:refs/heads/${each.value.branch}"
  bound_claims = {
    job_workflow_ref = "${each.value.repo_full}/.github/workflows/${each.value.wf_file}@refs/heads/${each.value.branch}"
  }

  access_token_ttl            = var.access_token_ttl_seconds
  access_token_max_ttl        = var.access_token_max_ttl_seconds
  access_token_num_uses_limit = 0
  access_token_trusted_ips    = [{ ip_address = "0.0.0.0/0" }]
}

resource "infisical_project_identity" "prod_viewer" {
  for_each = local.prod_workflows_flat

  identity_id = infisical_identity.prod[each.key].id
  project_id  = local.repo_project_uuids[each.value.repo_key]

  roles = [{ role_slug = "viewer" }]
}
