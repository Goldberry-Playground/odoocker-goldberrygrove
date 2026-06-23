# Infisical Identities — Terraform

Declaratively manages the per-workflow OIDC machine identities in Infisical Cloud that GitHub Actions workflows use to fetch secrets via workload identity federation (no long-lived secrets in GH Secrets).

Lives alongside the other TF envs in `infra/terraform/environments/`. Same `op run --env-file=.env.op --` credential injection pattern; state in the shared `grove-tf-state` Spaces bucket.

## What this manages

| Step | What | Resource |
|---|---|---|
| Per workflow | Identity at the Infisical org level | `infisical_identity` |
| Per workflow | OIDC auth method bound to that workflow's literal `workflow_ref` + branch | `infisical_identity_oidc_auth` |
| Per workflow | Project membership on `grove-odoocker` with `viewer` role (read-only secret access) | `infisical_project_identity` |

Currently scoped to 3 odoocker workflows:
- `sandbox-reaper.yml`
- `sandbox-deploy.yml`
- `release.yml`

The 4th odoocker workflow with OIDC (`terraform-drift.yml`) was created manually in the Infisical UI during Phase 3 of the OIDC retrofit (PR #40-42 timeline). It's intentionally left out of this initial TF apply — we'll migrate it into TF management in a follow-up PR via `terraform import` rather than re-creating it (which would change its UUID and force another workflow PR).

To add a new workflow: append one entry to the `odoocker_workflows` map in `variables.tf`, run `make infisical-identities-apply`, then commit the new identity UUID from `terraform output workflow_identity_ids` into the workflow's YAML.

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
