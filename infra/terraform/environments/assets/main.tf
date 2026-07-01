###############################################################################
# Grove Assets -- SHARED Spaces bucket + CDN + Cloudflare DNS for the
# `assets.gatheringatthegrove.com` subdomain.
#
# Distinct from every other TF env in this repo because assets are SHARED
# across local + QA (monolith + Level 3) + future prod. One bucket, one
# CDN endpoint, one DNS record; all envs read via NEXT_PUBLIC_ASSETS_URL.
#
# What lives here:
#   - hero images, brand illustrations, background photography per tenant
#   - anything referenced by frontend markup (`<Image src="/hero.jpg" />`)
#     that isn't a product photo (products live in Odoo) or blog image
#     (those live in Ghost)
#
# What does NOT live here:
#   - product photos          -> Odoo (URL pattern: ${ODOO_URL}${imageUrl})
#   - blog post images        -> Ghost (URL pattern: content API returns URLs)
#   - OpenObserve Parquet     -> obs-droplet MinIO OR DO Spaces (different bucket)
#   - TF remote state         -> grove-tf-state bucket
#
# Bucket layout (see var.tenant_prefixes):
#   grove-assets/hub/          <- hub site brand imagery
#   grove-assets/goldberry/    <- Goldberry Grove farm imagery
#   grove-assets/ggg/          <- GGG woodshop imagery
#   grove-assets/nursery/      <- At The Grove Nursery imagery
#   grove-assets/shared/       <- assets used by multiple tenants (e.g. Grove logo)
#
# Access model:
#   - Bucket ACL: public-read (assets are marketing content, no secrets)
#   - CORS: locked to frontend hostnames (var.cors_allowed_origins)
#   - Uploads: via digitalocean_spaces_key.assets_rw (readwrite grant)
#     used by scripts/upload-assets.sh (operator laptop)
#   - Frontends: no key needed -- CDN URL is public, cache-fronted
###############################################################################

provider "digitalocean" {
  token = var.do_token
}

# AWS provider aliased to the DO Spaces S3 endpoint -- used only for
# bucket_cors_configuration (DO provider doesn't expose CORS natively).
# Uses the assets_rw key created below.
provider "aws" {
  alias      = "spaces"
  region     = "us-east-1"
  access_key = digitalocean_spaces_key.assets_rw.access_key
  secret_key = digitalocean_spaces_key.assets_rw.secret_key

  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_region_validation      = true
  skip_requesting_account_id  = true

  endpoints {
    s3 = "https://${var.region}.digitaloceanspaces.com"
  }

  s3_use_path_style = true
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}

locals {
  assets_fqdn = "${var.assets_subdomain}.${var.cloudflare_zone_name}"
  tags = [
    "layer-assets",
    "project-grove",
  ]
}

data "cloudflare_zone" "apex" {
  name = var.cloudflare_zone_name
}

# ---- Spaces bucket ---------------------------------------------------------
#
# public-read ACL: marketing/brand assets are public by design. No secrets
# ever land in this bucket (see ARCHITECTURE.md / docs/ASSETS.md). Uploads
# happen via the operator-scoped key below.
#
# prevent_destroy: same defense as bootstrap/preview_data -- keeps a careless
# `terraform destroy` from wiping all assets. Removal requires editing this
# file first.

resource "digitalocean_spaces_bucket" "assets" {
  name   = var.bucket_name
  region = var.region
  acl    = "public-read"

  lifecycle {
    prevent_destroy = true
  }
}

# ---- Operator RW key (used by scripts/upload-assets.sh) --------------------

resource "digitalocean_spaces_key" "assets_rw" {
  name = "${var.bucket_name}-rw"

  grant {
    bucket     = digitalocean_spaces_bucket.assets.name
    permission = "readwrite"
  }
}

# ---- CORS ------------------------------------------------------------------
# Frontends fetch images from a different origin (their tenant hostname)
# than the bucket's CDN hostname. Browsers block cross-origin image reads
# unless the bucket returns Access-Control-Allow-Origin headers.
#
# Scoped to specific hostnames (var.cors_allowed_origins), not `*`, so a
# malicious page on some random domain can't hotlink Grove assets.

resource "aws_s3_bucket_cors_configuration" "assets" {
  provider = aws.spaces
  bucket   = digitalocean_spaces_bucket.assets.name

  cors_rule {
    allowed_methods = ["GET", "HEAD"]
    allowed_origins = var.cors_allowed_origins
    allowed_headers = ["*"]
    max_age_seconds = 3000
  }
}

# ---- DO CDN endpoint -------------------------------------------------------
# Fronts the bucket at the assets.<zone> hostname. DO's CDN handles TLS +
# global edge cache. TTL controls how long the edge holds each object;
# upload-assets.sh can override per-object via Cache-Control headers when
# an image needs to invalidate faster than TTL.

resource "digitalocean_cdn" "assets" {
  origin           = digitalocean_spaces_bucket.assets.bucket_domain_name
  ttl              = var.cdn_ttl_seconds
  certificate_name = null # Use DO-managed cert for the custom_domain below
  custom_domain    = local.assets_fqdn
}

# ---- Cloudflare CNAME → CDN endpoint ---------------------------------------
# Point assets.<zone> at the CDN. Cloudflare handles the outermost TLS +
# WAF; DO CDN handles the edge cache + origin fetch from the bucket.

resource "cloudflare_record" "assets" {
  zone_id = data.cloudflare_zone.apex.id
  name    = var.assets_subdomain
  type    = "CNAME"
  value   = digitalocean_cdn.assets.endpoint
  ttl     = 1    # 1 = "Auto" in Cloudflare, follows their edge TTL
  proxied = true # keeps traffic behind Cloudflare's WAF
}
