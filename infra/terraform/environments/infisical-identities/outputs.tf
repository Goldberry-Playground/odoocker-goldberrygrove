# Identity UUIDs to hardcode into workflow YAMLs as INFISICAL_IDENTITY_ID.
#
#   make infisical-identities-output
# or directly:
#   op run --env-file=.env.op -- terraform -chdir=infra/terraform/environments/infisical-identities output
#
# Not sensitive — these UUIDs are routing identifiers, not credentials.

output "prod_workflow_identity_ids" {
  description = "Map of prod-credential-workflow short-name → Infisical identity UUID. Hardcode into the workflow YAML's INFISICAL_IDENTITY_ID."
  value = {
    for k, _ in var.odoocker_prod_credential_workflows :
    k => infisical_identity.prod_workflow[k].id
  }
}

output "shared_readonly_identity_id" {
  description = "UUID of the single gh-oidc-odoocker-shared-readonly identity. ALL low-risk workflows (per var.odoocker_shared_readonly_workflows) hardcode this same value into their INFISICAL_IDENTITY_ID."
  value       = infisical_identity.shared_readonly.id
}

output "shared_readonly_consumers" {
  description = "Workflows expected to use the shared-readonly identity (informational; not enforced at the trust-policy level)."
  value       = var.odoocker_shared_readonly_workflows
}
