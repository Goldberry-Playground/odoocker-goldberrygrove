output "bucket_name" {
  description = "Spaces bucket name (e.g. grove-assets). Used by upload-assets.sh + any operator running `s3cmd`/`mc` commands."
  value       = digitalocean_spaces_bucket.assets.name
}

output "bucket_origin" {
  description = "Direct bucket URL. Used internally by the CDN; operators shouldn't hit this URL directly (it bypasses the edge cache + Cloudflare WAF)."
  value       = digitalocean_spaces_bucket.assets.bucket_domain_name
}

output "assets_url" {
  description = "Public frontend URL. Wire into every grove-sites app as NEXT_PUBLIC_ASSETS_URL. Frontend code fetches at $${assets_url}/$${tenant}/$${path}."
  value       = "https://${local.assets_fqdn}"
}

output "cdn_endpoint" {
  description = "Raw DO CDN edge hostname (before Cloudflare). Reference only; use assets_url for actual fetches."
  value       = digitalocean_cdn.assets.endpoint
}

output "operator_access_key_id" {
  description = "Spaces access key ID for uploads. Push to 1Password under GoldberryGrove Infra / grove_assets_access_key_id, then upload-assets.sh reads from there."
  value       = digitalocean_spaces_key.assets_rw.access_key
  sensitive   = true
}

output "operator_secret_key" {
  description = "Spaces secret key for uploads. Push to 1Password under GoldberryGrove Infra / grove_assets_secret_key. NEVER commit; operators fetch via op read."
  value       = digitalocean_spaces_key.assets_rw.secret_key
  sensitive   = true
}

output "social_ingest_access_key_id" {
  description = "Spaces access key ID for the discord-bridge social-ingest store (GOL-1120). Push to 1Password under Grove Infra / grove_asset_store_key; injected into the bridge as GROVE_ASSET_STORE_KEY."
  value       = digitalocean_spaces_key.assets_social_rw.access_key
  sensitive   = true
}

output "social_ingest_secret_key" {
  description = "Spaces secret key for the discord-bridge social-ingest store (GOL-1120). Push to 1Password under Grove Infra / grove_asset_store_secret; injected as GROVE_ASSET_STORE_SECRET. NEVER commit."
  value       = digitalocean_spaces_key.assets_social_rw.secret_key
  sensitive   = true
}
