###############################################################################
# Grove Cutover Archives — the shared, private Spaces bucket that holds
# pre-migration data-cutover archives (rollback + audit surface).
#
# Why this env exists (GOL-2101):
#   The `grove-cutover-archives` bucket was created imperatively via the Spaces
#   API to unblock the WS3a Asana pre-migration archive gate (GOL-2093). This
#   env codifies it so it is not a snowflake — the bucket, its versioning, its
#   private ACL, and its lifecycle policy now live in version control and
#   converge from `terraform apply`.
#
# What lives in this bucket:
#   grove-cutover-archives/asana/    <- full Asana workspace JSON export (GOL-2093)
#   grove-cutover-archives/square/   <- future: Square data cutover archive
#   grove-cutover-archives/venmo/    <- future: Venmo data cutover archive
#
#   Each prefix is a point-in-time, pre-migration snapshot kept as the
#   rollback/audit source-of-truth for that system's migration into Odoo.
#
# What does NOT live here:
#   - TF remote state          -> grove-tf-state bucket
#   - marketing/brand assets    -> grove-assets bucket (public-read + CDN)
#   - product photos            -> Odoo
#
# Access model:
#   - Bucket ACL: PRIVATE. These archives may contain internal task notes,
#     assignee data, and other non-public content. No CDN, no public read,
#     no Cloudflare fronting — intentionally the opposite of grove-assets.
#   - Versioning: ENABLED. An archive is a rollback surface; a clobbered or
#     re-uploaded object must remain point-in-time recoverable.
#   - Writes: via the same bootstrap Spaces key the operator already uses for
#     bucket ops (fed by .env.op). No dedicated app runtime consumes this
#     bucket, so no service-scoped key is provisioned here (least privilege:
#     nothing long-lived needs standing access). Add a bucket-scoped
#     digitalocean_spaces_key here if/when an automated writer appears.
#
# Apply/import note: the bucket already EXISTS (created imperatively). Codifying
# it requires a one-time `terraform import` before the first apply — see
# README.md. After import, `terraform plan` should show at most the versioning
# + lifecycle configuration converging, never a bucket create/destroy.
###############################################################################

provider "digitalocean" {
  token = var.do_token

  # Spaces creds are needed for bucket-level operations (create, refresh,
  # versioning, lifecycle). The DO REST API token manages the DO account but
  # NOT S3-protocol bucket resources — those need S3-style creds. Same
  # long-lived "plumbing" key the state-backend env uses. See state-backend
  # README "Why two Spaces keys".
  spaces_access_id  = var.spaces_bootstrap_access_key_id
  spaces_secret_key = var.spaces_bootstrap_secret_key
}

# ---- The cutover-archives bucket -------------------------------------------
#
# private ACL: archives are internal audit/rollback data, not public content.
#
# versioning: point-in-time recovery. Re-uploading or clobbering an archive
# object leaves the prior version recoverable. This is a non-destructive,
# in-place change — enabling it never rewrites existing objects, it only starts
# versioning writes from here forward. (Matches grove-tf-state's rationale.)
#
# lifecycle_rule: keep CURRENT versions indefinitely (the live archive), but
# expire NON-current versions after var.noncurrent_version_expiration_days so
# accumulated re-uploads don't grow storage without bound. Abort half-finished
# multipart uploads (e.g. an interrupted large export upload) after 7 days —
# pure waste otherwise.
#
# prevent_destroy: same defense as grove-tf-state / grove-assets — keeps a
# careless `terraform destroy` from wiping the rollback/audit surface. Removal
# requires editing this file first.
resource "digitalocean_spaces_bucket" "cutover_archives" {
  name   = var.bucket_name
  region = var.region
  acl    = "private"

  versioning {
    enabled = true
  }

  lifecycle_rule {
    id      = "expire-noncurrent-archive-versions"
    enabled = true

    noncurrent_version_expiration {
      days = var.noncurrent_version_expiration_days
    }

    abort_incomplete_multipart_upload_days = 7
  }

  lifecycle {
    prevent_destroy = true
  }
}
