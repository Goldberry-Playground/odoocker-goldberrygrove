output "blogs_droplet_ip" {
  description = "Ephemeral public IPv4 of the blogs droplet. Changes on every replace - do NOT point DNS at it (that was the GOL-387 bug); use blogs_reserved_ip. Kept for SSH/debugging."
  value       = digitalocean_droplet.blogs.ipv4_address
}

output "blogs_reserved_ip" {
  description = "Stable reserved IP for the blogs droplet. The blog.* A records (and the apex records, when they land) point HERE, so an immutable droplet replace needs no DNS change (GOL-387)."
  value       = digitalocean_reserved_ip.blogs.ip_address
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
  description = "Ephemeral public IPv4 of the Odoo droplet itself. CHANGES ON EVERY DROPLET REPLACE - do not pin anything to it (that was the GOL-382 bug). DNS points at odoo_reserved_ip; use this only for direct droplet SSH/debug."
  value       = digitalocean_droplet.odoo.ipv4_address
}

output "odoo_reserved_ip" {
  description = "Stable reserved IPv4 the odoo.gatheringatthegrove.com A record points at (GOL-382). Survives a droplet replace, which is what makes the #242 day-2 immutable-replace model a bounded ~10-min window with NO DNS change. This is the origin address Cloudflare dials; the record is proxied, so it is not user-visible."
  value       = digitalocean_reserved_ip.odoo.ip_address
}

output "odoo_backups_bucket" {
  description = "Spaces bucket holding the nightly Odoo filestore mirror (GOL-99): filestore/current/ (live mirror), filestore/archive/ (deletions, 35d), filestore/manifest/ (per-run counts). Restore procedure: docs/RUNBOOK-odoo-filestore-restore.md."
  value       = digitalocean_spaces_bucket.odoo_backups.name
}

output "odoo_hostname" {
  description = "Canonical Odoo URL. App Platform frontends call this for the headless REST API."
  value       = "https://${local.odoo_host}"
}

output "odoo_filestore_volume_id" {
  description = "Volume ID of the durable Odoo filestore (/var/lib/odoo) block volume (GOL-93). GOL-99 wires the nightly backup into this volume."
  value       = digitalocean_volume.odoo_filestore.id
}

# === Track 2 (ADR-007 Phase 6, GOL-116) - App Platform frontends ============
# No custom-domain outputs yet: the apex cutover is deferred (domain{} blocks
# omitted in apps.tf). Until then, probe/verify the apps on their default
# *.ondigitalocean.app ingress URLs below.

output "hub_app_id" {
  description = "App Platform app ID for the hub. Used by post-deploy verification (doctl apps get <id>), Keep alert routing, and deploy-status polling."
  value       = digitalocean_app.hub.id
}

output "hub_default_ingress" {
  description = "Default *.ondigitalocean.app ingress App Platform assigns the hub. Hit this to verify the app is ACTIVE + serving 200 before the (deferred) apex cutover."
  value       = digitalocean_app.hub.live_url
}

output "tenant_app_ids" {
  description = "App Platform app IDs per tenant (goldberry / ggg / nursery). Same uses as hub_app_id."
  value       = { for k, app in digitalocean_app.tenant : k => app.id }
}

output "tenant_default_ingresses" {
  description = "Default *.ondigitalocean.app ingress URLs per tenant, for probing before the deferred apex cutover."
  value       = { for k, app in digitalocean_app.tenant : k => app.live_url }
}
