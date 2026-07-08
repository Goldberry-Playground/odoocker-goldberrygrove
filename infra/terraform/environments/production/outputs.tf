output "blogs_droplet_ip" {
  description = "Public IPv4 of the blogs droplet (apex + blog.* records point here)."
  value       = digitalocean_droplet.blogs.ipv4_address
}

output "blog_urls" {
  description = "The four blog.* hostnames."
  value       = { for k, z in local.tenants : k => "https://blog.${z}" }
}

output "backups_bucket" {
  description = "Spaces bucket receiving nightly blog backups."
  value       = digitalocean_spaces_bucket.blogs_backups.name
}

# === Track 2 (ADR-007 Phase 6, GOL-105) - Managed PG + Odoo droplet =========

output "pg_cluster_id" {
  description = "Managed PG cluster ID. Used by post-apply verification (doctl databases) and any future App Platform managed-resource attachment."
  value       = digitalocean_database_cluster.pg.id
}

output "pg_private_host" {
  description = "Private VPC hostname of the Managed PG cluster. The Odoo droplet connects here over the private network (no public DB exposure)."
  value       = digitalocean_database_cluster.pg.private_host
  sensitive   = true
}

output "pg_database_name" {
  description = "Name of the Odoo database in the Managed PG cluster."
  value       = digitalocean_database_db.odoo.name
}

output "odoo_droplet_ip" {
  description = "Public IPv4 of the Odoo droplet (odoo.gatheringatthegrove.com A record points here; Cloudflare-proxied)."
  value       = digitalocean_droplet.odoo.ipv4_address
}

output "odoo_hostname" {
  description = "Canonical Odoo URL. App Platform frontends call this for the headless REST API."
  value       = "https://${local.odoo_host}"
}

output "odoo_filestore_volume_id" {
  description = "Volume ID of the durable Odoo filestore (/var/lib/odoo) block volume (GOL-93). GOL-99 wires the nightly backup into this volume."
  value       = digitalocean_volume.odoo_filestore.id
}
