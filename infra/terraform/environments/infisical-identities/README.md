# Infisical Identities — Terraform

Declaratively manages the OIDC machine identities in Infisical Cloud that GitHub Actions workflows use to fetch secrets via workload identity federation (no long-lived secrets in GH Secrets).

Lives alongside the other TF envs in `infra/terraform/environments/`. Same `op run --env-file=.env.op --` credential injection pattern; state in the shared `grove-tf-state` Spaces bucket.

## What this manages — two-tier identity model

Imposed by the Infisical free-tier 5-identity cap (decision 2026-06-23). Two tiers of identity, deliberately:

### Tier 1: per-workflow identities for prod-credential workflows (`var.odoocker_prod_credential_workflows`)

| Resource | Per workflow | Trust policy |
|---|---|---|
| `infisical_identity.prod_workflow` | 1 | Org role `no-access` |
| `infisical_identity_oidc_auth.prod_workflow` | 1 | STRICT — pins `repo+ref` AND `workflow_ref` to a specific file |
| `infisical_project_identity.prod_workflow_viewer` | 1 | Viewer on grove-odoocker |

Today: `release.yml` only. A compromise of any non-release workflow CANNOT use this identity.

### Tier 2: ONE shared-readonly identity for low-risk workflows (`var.odoocker_shared_readonly_workflows`)

| Resource | Count | Trust policy |
|---|---|---|
| `infisical_identity.shared_readonly` | 1 | Org role `no-access` |
| `infisical_identity_oidc_auth.shared_readonly` | 1 | LOOSE — pins `repo+ref` only, NO `workflow_ref` claim |
| `infisical_project_identity.shared_readonly_viewer` | 1 | Viewer on grove-odoocker |

Today's consumers: `terraform-drift.yml`, `sandbox-reaper.yml`, `sandbox-deploy.yml`. All hardcode the SAME `INFISICAL_IDENTITY_ID` (the shared identity's UUID).

**Blast-radius justification:** all three already read the same secrets via per-workflow identities pre-compromise. Sharing doesn't expand exposure. The risk is "future workflow added to odoocker:main without explicit identity gets free access" — mitigated by code review + the workflow YAML pattern being well-documented.

### Adding workflows

- **New prod-credential workflow** (e.g. a future prod-CD): append to `odoocker_prod_credential_workflows` in `variables.tf`. Each entry consumes 1 identity slot.
- **New low-risk workflow**: append to `odoocker_shared_readonly_workflows` (informational) and update the workflow YAML to reference the shared identity's UUID. Zero new identity slots consumed.

## Pre-existing identities to delete before first apply

Three identities created earlier in the day (during Phase 3 + the script-driven sandbox-reaper attempt) need to be deleted in the Infisical UI before `terraform apply`:

1. **`gh-oidc-odoocker-terraform-drift`** (manually created in UI during PR #40-42) — replaced by `gh-oidc-odoocker-shared-readonly`
2. **`gh-oidc-odoocker-sandbox-reaper`** (script-created during PR #47 testing) — replaced by `gh-oidc-odoocker-shared-readonly`
3. **`seed-script-odoocker`** (Universal Auth, used by `make infisical-seed`) — drop and use `tf-infisical-admin` for one-time seed operations (it has Admin on grove-odoocker)

Delete via: Infisical UI → Org → Access Control → Identities → click each → delete. Re-running `make infisical-seed` after dropping seed-script-odoocker: temporarily update `.env.infisical-seed.op.example` to point at `infisical_admin_client_id` / `infisical_admin_client_secret`, run, revert.

## Prerequisites — the irreducible trust roots

These get done ONCE per Infisical org. After that this TF env handles everything else.

1. **Infisical Cloud org exists** — created 2026-06-18 per `memory/reference_infisical_cloud_org.md` (id `952236a8-4ed4-45c0-81e8-5157b48557a2`)
2. **`grove-odoocker` project exists** — created 2026-06-23, UUID `850603f8-e175-4c38-9038-97a1e69d72e6`
3. **`tf-infisical-admin` identity + Universal Auth credentials exist** — created by `make infisical-admin-bootstrap` (PR #44/#45). Client ID + Secret live in 1Password `GoldberryGrove Infra`.
4. **`grove-tf-state` Spaces bucket exists** — provisioned by `state-backend/` TF env. Same bucket that backs every other TF env's state.
5. **Bootstrap Spaces keys in 1Password** — `spaces_bootstrap_access_key_id` + `spaces_bootstrap_secret_key`. The S3 backend uses these to read/write state. Same keys used by every other env.

## First apply

```bash
cd infra/terraform/environments/infisical-identities

# One-time: copy the backend template
cp backend.hcl.example backend.hcl   # backend.hcl is git-ignored

# Init + apply via op run (credentials never touch shell history / argv)
op run --env-file=.env.op -- terraform init -backend-config=backend.hcl
op run --env-file=.env.op -- terraform plan
op run --env-file=.env.op -- terraform apply

# Or, from the repo root, use the Makefile targets:
make infisical-identities-init
make infisical-identities-plan
make infisical-identities-apply
```

Expected output: `Apply complete! Resources: 9 added, 0 changed, 0 destroyed.` (3 workflows × 3 resources each).

## Reading the outputs

After apply, the per-workflow identity UUIDs are needed to update the workflow YAML files (as the `INFISICAL_IDENTITY_ID` env var that `Infisical/secrets-action` consumes).

```bash
make infisical-identities-output
# or:
op run --env-file=.env.op -- terraform -chdir=infra/terraform/environments/infisical-identities \
  output -json workflow_identity_ids
```

Then update each workflow's `INFISICAL_IDENTITY_ID` env var to the UUID from the output. The UUIDs are non-sensitive routing values; commit them as literals.

## Rotating credentials / changing trust policy

To rotate a trust-policy literal (e.g., move a workflow from `main` to `release` branch):
1. Update `variables.tf` (or pass `-var github_branch=...`)
2. `make infisical-identities-apply`
3. The OIDC resource updates in place; identity UUID unchanged

To rotate the `tf-infisical-admin` Client Secret:
1. Re-run `make infisical-admin-bootstrap` — script detects existing identity, skips creation, mints a fresh Client Secret, updates 1Password
2. Next `terraform plan/apply` picks up the new secret from 1Password automatically

## Caveats

### Secrets in tfstate

The Infisical provider's auth credentials, the access tokens it mints during apply, and the trust-policy literals all live in `terraform.tfstate`. State file is in `grove-tf-state` Spaces bucket — protect the bootstrap Spaces keys accordingly.

### Trust-policy literals matter

The `bound_subject` + `bound_claims.job_workflow_ref` literals are what tie a workflow's OIDC token to a specific identity. Renaming a workflow file, moving it between branches, or moving it to a different repo will break the trust policy until this TF env is updated. There's no glob fallback by design — per `memory/feedback_oidc_trust_policy_pattern.md`, hardcoded literals only. The maintenance cost is the same as renaming a file in CI generally.

### Adding new workflows

Add to the `odoocker_workflows` map. The `for_each` creates all three resources for the new entry on next apply. No other code changes needed.

## What's NOT here

- **`terraform-drift.yml` identity** — manually created in UI during Phase 3, intentionally left out of this PR. Migration via `terraform import` is a follow-up.
- **grove-sites workflow identities** — `preview-up.yml`, `preview-down.yml`, etc. Will be added in a separate TF env (or as a second `for_each` map in this one) when grove-sites OIDC retrofit kicks off. The pattern is identical; only `github_repo_full_name` + the workflow list differ.

## Related

- `scripts/infisical-admin-bootstrap.sh` — one-shot creation of the admin identity this env uses for auth
- `memory/project_infisical_decision_cloud.md` — the broader OIDC retrofit decision record
- `memory/feedback_oidc_trust_policy_pattern.md` — trust-policy pattern (literals, no actor binding)
- `memory/feedback_infisical_uuid_vs_slug.md` — when to use UUID vs slug across tools
- `docs/ADR/003-infisical-secrets-broker.md` — full architecture decision record
