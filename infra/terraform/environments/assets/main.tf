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
#
# 2026-07-01: DOES NOT use the TF-created assets_rw key (that created a
# chicken-and-egg: newly-created key returns 403 on CORS PUT within the
# same apply run). Instead, this uses the BOOTSTRAP Spaces key already
# in the operator's env via SPACES_ACCESS_KEY_ID / SPACES_SECRET_ACCESS_KEY
# (fed by .env.op). Same key that talks to grove-tf-state backend.
# AWS provider reads AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY env vars by
# default -- .env.op maps those to the bootstrap Spaces key.
provider "aws" {
  alias  = "spaces"
  region = "us-east-1"

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

# ---- DNS: delegate assets.<apex> to DO --------------------------------------
# History of this hop (see git log for the full arc):
#   2026-07-01: CDN custom_domain failed on first apply (LE cert needs DNS
#               first -- chicken-and-egg on an empty state). Fell back to a
#               Cloudflare PROXIED CNAME at the vanity host.
#   2026-07-02: The proxied CNAME turned out to 403 -- Cloudflare preserves
#               the original Host header on the origin fetch and the DO CDN
#               (no custom_domain) doesn't recognize the vanity hostname.
#               CF host-header override is Enterprise-only, and DO's LE
#               issuance only works for domains in DO-managed DNS.
#   Fix: NS-delegate the assets subdomain to DO (same pattern as the qa-l3
#   zone in ../qa-app-platform/main.tf), let DO own cert + routing.
#
# Nothing user-facing breaks during the cutover window: images currently use
# the raw CDN hostname (grove-sites bakes NEXT_PUBLIC_ASSETS_URL to it), so
# the vanity host has no traffic until this lands + grove-sites flips.

resource "digitalocean_domain" "assets_zone" {
  name = local.assets_fqdn
}

# NS records under the apex Cloudflare zone hand DNS for the assets
# subdomain over to DO's nameservers. Replaces the proxied CNAME that
# previously lived at this name -- CNAMEs can't coexist with NS records,
# so if a single apply races the delete/create, re-run the apply once.
resource "cloudflare_record" "assets_ns" {
  for_each = toset([
    "ns1.digitalocean.com",
    "ns2.digitalocean.com",
    "ns3.digitalocean.com",
  ])

  zone_id = data.cloudflare_zone.apex.id
  name    = var.assets_subdomain
  type    = "NS"
  value   = each.value
  ttl     = 1 # 1 = Cloudflare "Auto"
}

# LE cert for the vanity host, issued by DO. Works ONLY because the domain
# above is DO-managed -- DO writes its own validation records. depends_on
# the NS delegation so LE's resolver can chase apex -> DO nameservers on
# first issuance. If the very first apply fails with a validation timeout,
# the delegation hadn't propagated yet: re-run the apply.
resource "digitalocean_certificate" "assets" {
  name    = "grove-assets-vanity"
  type    = "lets_encrypt"
  domains = [local.assets_fqdn]

  depends_on = [
    digitalocean_domain.assets_zone,
    cloudflare_record.assets_ns,
  ]

  lifecycle {
    create_before_destroy = true
  }
}

# ---- DO CDN endpoint -------------------------------------------------------
# Fronts the bucket at the DO-provisioned CDN endpoint. TTL controls how
# long the edge holds each object; upload-assets.sh can override per-object
# via Cache-Control headers when an image needs to invalidate faster.
#
# custom_domain makes the CDN answer to the vanity host directly (TLS via
# the DO-issued LE cert above). DO manages the resolution records inside
# the delegated assets zone itself.

resource "digitalocean_cdn" "assets" {
  origin           = digitalocean_spaces_bucket.assets.bucket_domain_name
  ttl              = var.cdn_ttl_seconds
  custom_domain    = local.assets_fqdn
  certificate_name = digitalocean_certificate.assets.name
}

# ---- Apex resolution inside the delegated zone -------------------------------
# DO does NOT auto-create the resolution record for CDN custom domains added
# via the API (the control-panel flow does, but only when the PARENT zone is
# in DO). And our vanity host is the APEX of the delegated zone, where CNAME
# is forbidden ("CNAME records cannot share a name with other records" --
# verified against the DO API 2026-07-02).
#
# So: A records at the apex, pointing at the CDN endpoint's edge IPs. The
# hashicorp/dns data source resolves the endpoint at plan time, so the
# records track edge-IP changes on every apply instead of hardcoding.
#
# Brittleness, acknowledged: if DO/Cloudflare re-homes the CDN endpoint's
# anycast IPs BETWEEN applies, the vanity host breaks until the next apply.
# Acceptable for marketing assets because (a) anycast assignments are
# long-lived in practice, (b) grove-sites can flip NEXT_PUBLIC_ASSETS_URL
# back to the raw CDN hostname (always valid) as an instant mitigation,
# and (c) terraform-drift.yml re-plans on schedule and will surface the IP
# drift as a pending change.

data "dns_a_record_set" "cdn_edge" {
  host = digitalocean_cdn.assets.endpoint
}

resource "digitalocean_record" "assets_apex" {
  for_each = toset(data.dns_a_record_set.cdn_edge.addrs)

  domain = digitalocean_domain.assets_zone.name
  type   = "A"
  name   = "@"
  value  = each.value
  ttl    = 300
}
