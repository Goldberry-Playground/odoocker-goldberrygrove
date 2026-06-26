output "app_droplet_id" {
  description = "Production app Droplet ID"
  value       = module.app.droplet_id
}

output "app_ipv4_address" {
  description = "Public IPv4 address of the production app Droplet"
  value       = module.app.ipv4_address
}

output "monitoring_droplet_id" {
  description = "Production monitoring Droplet ID"
  value       = module.monitoring.droplet_id
}

output "monitoring_ipv4_address" {
  description = "Public IPv4 address of the production monitoring Droplet"
  value       = module.monitoring.ipv4_address
}

output "app_volume_id" {
  description = "Block storage volume attached to the app Droplet"
  value       = module.app.volume_id
}

output "monitoring_volume_id" {
  description = "Block storage volume attached to the monitoring Droplet"
  value       = module.monitoring.volume_id
}

# TODO(M4): re-add the dns_records output once digitalocean_record.app exists
# in main.tf. The output was added speculatively before the resource was
# declared, so `terraform validate` errors with "Reference to undeclared
# resource" -- pre-existing on main; surfaced 2026-06-26 when ci.yml's new
# terraform-checks job ran across all envs for the first time.
# output "dns_records" {
#   description = "Map of hostname to A record value"
#   value = {
#     for k, r in digitalocean_record.app : k => r.value
#   }
# }
