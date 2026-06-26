output "droplet_ipv4" {
  description = "Public IPv4 of the QA droplet. SSH: ssh -i ~/.ssh/grove-qa-deploy root@<this>"
  value       = digitalocean_droplet.qa.ipv4_address
}

output "qa_zone" {
  description = "The delegated QA DNS zone (qa.gatheringatthegrove.com by default)."
  value       = digitalocean_domain.qa.name
}

output "qa_urls" {
  description = "Public URLs operators / testers hit. Resolved by Cloudflare → DO delegation. TLS via Let's Encrypt (Caddy with DO DNS-01)."
  value = merge(
    {
      hub = "https://${digitalocean_domain.qa.name}"
    },
    {
      for sub in var.tenant_subdomains :
      sub => "https://${sub}.${digitalocean_domain.qa.name}"
    }
  )
}

output "ssh_command" {
  description = "Quick-copy SSH command to connect to the QA droplet for troubleshooting."
  value       = "ssh -i ~/.ssh/grove-qa-deploy root@${digitalocean_droplet.qa.ipv4_address}"
}

output "ssh_key_fingerprint" {
  description = "DO SSH key fingerprint (in case another env needs to attach the same key)."
  value       = digitalocean_ssh_key.qa_deploy.fingerprint
}
