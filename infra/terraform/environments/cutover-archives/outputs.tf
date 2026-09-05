output "bucket_name" {
  description = "Spaces bucket name (grove-cutover-archives). Operators write archives via `s3cmd`/`mc`/`aws s3` under per-system prefixes (asana/, square/, venmo/)."
  value       = digitalocean_spaces_bucket.cutover_archives.name
}

output "bucket_region" {
  description = "Spaces region the bucket lives in (nyc3)."
  value       = digitalocean_spaces_bucket.cutover_archives.region
}

output "bucket_domain_name" {
  description = "Direct S3-style bucket URL (private; requires signed requests). Reference only — there is no CDN or public edge in front of this bucket by design."
  value       = digitalocean_spaces_bucket.cutover_archives.bucket_domain_name
}
