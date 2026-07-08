output "geo_block_rulesets" {
  description = "Ruleset IDs per zone, for dashboard cross-referencing and API verification (GET /zones/<zone_id>/rulesets/<id>)."
  value       = { for name, rs in cloudflare_ruleset.geo_block : name => rs.id }
}

output "blocked_countries" {
  description = "Country codes currently blocked at the edge."
  value       = sort(tolist(var.blocked_countries))
}

output "odoo_image_cache_ruleset_id" {
  description = "Ruleset ID of the Odoo /web/image/* edge-cache rule (GOL-93). Verify via GET /zones/<zone_id>/rulesets/<id> or the cf-cache-status response header on odoo.gatheringatthegrove.com/web/image/*."
  value       = cloudflare_ruleset.odoo_image_cache.id
}
