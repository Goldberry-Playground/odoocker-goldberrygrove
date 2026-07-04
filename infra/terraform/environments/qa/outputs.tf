output "droplet_ipv4" {
  description = "Public IPv4 of the QA droplet. SSH: ssh -i ~/.ssh/grove-qa-deploy root@<this>"
  value       = digitalocean_droplet.qa.ipv4_address
}

# (qa_zone + qa_urls outputs removed 2026-07-04 -- the qa zone moved to
# ../qa-app-platform/, which now serves the public qa.* URLs. This env only
# holds the fallback droplet until final teardown.)

output "ssh_command" {
  description = "Quick-copy SSH command to connect to the QA droplet for troubleshooting."
  value       = "ssh -i ~/.ssh/grove-qa-deploy root@${digitalocean_droplet.qa.ipv4_address}"
}

output "ssh_key_fingerprint" {
  description = "DO SSH key fingerprint (in case another env needs to attach the same key)."
  value       = digitalocean_ssh_key.qa_deploy.fingerprint
}

output "caddy_data_volume_id" {
  description = "DO block-storage volume ID holding Caddy /data (LE certs). To manually destroy: terraform destroy -target=digitalocean_volume.caddy_data."
  value       = digitalocean_volume.caddy_data.id
}
