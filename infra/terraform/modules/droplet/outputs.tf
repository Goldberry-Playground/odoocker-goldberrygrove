output "droplet_id" {
  description = "DigitalOcean Droplet ID"
  value       = digitalocean_droplet.this.id
}

output "ipv4_address" {
  description = "Public IPv4 address of the Droplet"
  value       = digitalocean_droplet.this.ipv4_address
}

output "volume_id" {
  description = "DigitalOcean Block Storage volume ID"
  value       = digitalocean_volume.this.id
}
