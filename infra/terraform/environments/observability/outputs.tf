output "obs_droplet_ip" {
  description = "Public IPv4 of the observability droplet."
  value       = module.obs_droplet.ipv4_address
}

output "obs_droplet_id" {
  description = "ID of the observability droplet."
  value       = module.obs_droplet.droplet_id
}

# ── Public RUM ingest (GOL-311) ──────────────────────────────────────────────
# The A record for rum_public_host must point (orange-cloud) at obs_droplet_ip.
output "rum_public_host" {
  description = "Browser-facing RUM hostname. Set grove-sites NEXT_PUBLIC_OO_RUM_SITE to exactly this (host only, no scheme)."
  value       = var.rum_public_host
}

output "rum_ingest_url" {
  description = "Full RUM ingest URL the @openobserve/browser-rum SDK POSTs to (org=default, stream=rum). Handy for a curl/OPTIONS smoke test."
  value       = "https://${var.rum_public_host}/rum/v1/default/rum"
}

output "openobserve_url" {
  description = "OpenObserve UI (admin-only via the firewall)."
  value       = "http://${module.obs_droplet.ipv4_address}:5080"
}

output "keep_url" {
  description = "Keep UI (admin-only via the firewall)."
  value       = "http://${module.obs_droplet.ipv4_address}:3034"
}

# Keep webhook/API base — the target for OpenObserve's `keep-webhook` alert
# destination (set KEEP_EVENT_URL to this + /alerts/event?provider_id=openobserve).
# Uses the PUBLIC IP on purpose: OO v0.91.1's SSRF guard rejects a private-IP
# destination at create time, so the OO->Keep POST must resolve to the public IP
# (host-local hairpin). Admin-only + X-API-KEY (WEBHOOK_TOKEN) gated. (GOL-279)
output "keep_webhook_url" {
  description = "Keep webhook/API base (public IP:8080) — OpenObserve alert-destination target; SSRF-safe, admin-only + X-API-KEY gated."
  value       = "http://${module.obs_droplet.ipv4_address}:8080"
}
