# Per-workflow identity UUIDs. These are the values that workflow YAML files
# reference as `identity-id` in the Infisical/secrets-action call.
#
# Read with:
#   make infisical-identities-output
#   # or directly:
#   op run --env-file=.env.op -- terraform -chdir=infra/terraform/environments/infisical-identities output workflow_identity_ids
#
# Not sensitive — these UUIDs are routing identifiers, not credentials.

output "workflow_identity_ids" {
  description = "Map of workflow short-name → Infisical identity UUID. Hardcode these into the workflow YAML files as INFISICAL_IDENTITY_ID."
  value = {
    for k, _ in var.odoocker_workflows :
    k => infisical_identity.odoocker_workflow[k].id
  }
}

output "workflow_identity_names" {
  description = "Map of workflow short-name → human-readable identity name as it appears in Infisical UI."
  value = {
    for k, _ in var.odoocker_workflows :
    k => infisical_identity.odoocker_workflow[k].name
  }
}
