output "geo_block_rulesets" {
  description = "Ruleset IDs per zone, for dashboard cross-referencing and API verification (GET /zones/<zone_id>/rulesets/<id>)."
  value       = { for name, rs in cloudflare_ruleset.geo_block : name => rs.id }
}

output "blocked_countries" {
  description = "Country codes currently blocked at the edge."
  value       = sort(tolist(var.blocked_countries))
}

output "otlp_ingest_bearer_gate" {
  description = "OTLP ingest Bearer gate status (GOL-2330). host = the CF-proxied ingest hostname the WAF rule guards; active = whether the Bearer rule is rendered (true once var.otlp_ingest_bearer_token is set). Verify at the edge: authorized `Authorization: Bearer <token>` -> reaches origin; missing/wrong -> 403. Token value is intentionally NOT exported."
  value = {
    host   = var.otlp_ingest_host
    active = nonsensitive(trimspace(var.otlp_ingest_bearer_token) != "")
    dns    = var.otlp_ingest_origin_ip != ""
  }
}

output "odoo_image_cache_ruleset_id" {
  description = "Ruleset ID of the Odoo /web/image/* edge-cache rule (GOL-93). Verify via GET /zones/<zone_id>/rulesets/<id> or the cf-cache-status response header on odoo.gatheringatthegrove.com/web/image/*."
  value       = cloudflare_ruleset.odoo_image_cache.id
}
