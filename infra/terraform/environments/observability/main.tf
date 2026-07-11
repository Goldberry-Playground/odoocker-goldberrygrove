###############################################################################
# Grove Observability — dedicated obs-droplet environment.
#
# Provisions the SEPARATE observability plane from the design spec (§4–6): a
# droplet running OpenObserve + Keep, deliberately isolated from the app plane
# so monitoring outlives an app outage. OpenObserve writes Parquet to DO Spaces
# (S3-compatible) — there is no shared MinIO here (that's the app plane).
#
# Lifecycle:  `terraform apply` provisions/updates in place.
# Cost:       ~$12–24/mo (droplet) + Spaces (shared). See README.md.
#
# STATUS: APPLIED 2026-07-11 (GOL-270). grove-obs droplet 583893144 (nyc3,
# s-2vcpu-4gb, 161.35.186.49) is live; state at s3://grove-tf-state/
# observability/terraform.tfstate. Remaining live wiring (agenticos collector
# enablement GOL-54, setup-monitoring.py alert bootstrap, Keep :8080 API
# reachability) tracked in README.md → Follow-ups.
###############################################################################

provider "digitalocean" {
  token = var.do_token

  # Spaces (S3) creds so this env can TF-manage the OpenObserve Parquet bucket
  # below. Same DO Spaces keys OpenObserve itself uses (var.spaces_*).
  spaces_access_id  = var.spaces_access_key
  spaces_secret_key = var.spaces_secret_key
}

# OpenObserve Parquet storage bucket (S3-compatible DO Spaces). Codified here so
# the bucket is idempotent and torn down with the env — not click-created. The
# obs droplet's ZO_S3_BUCKET_NAME (cloud-init) points at this exact name.
resource "digitalocean_spaces_bucket" "obs" {
  name   = var.spaces_bucket
  region = var.region
  acl    = "private"
}

locals {
  tags = [
    "env-observability",
    "project-grove",
    "plane-observability",
  ]
}

# CI key TF-manages (long-lived, per the qa env's PR #63 rationale).
resource "digitalocean_ssh_key" "obs_deploy" {
  name       = "grove-obs-deploy"
  public_key = var.ci_ssh_public_key
}

# Admin key managed out-of-band — referenced, never created/destroyed here.
data "digitalocean_ssh_key" "obs_admin" {
  name = var.admin_ssh_key_name
}

# Obs droplet via the shared droplet module (droplet + optional volume). No
# attached volume: OpenObserve Parquet lives in Spaces; Keep's SQLite is small
# and fits local disk.
module "obs_droplet" {
  source = "../../modules/droplet"

  name           = "grove-obs"
  size           = var.droplet_size
  region         = var.region
  image          = var.droplet_image
  volume_size_gb = 0
  monitoring     = true
  tags           = local.tags

  ssh_key_ids = [
    digitalocean_ssh_key.obs_deploy.fingerprint,
    data.digitalocean_ssh_key.obs_admin.fingerprint,
  ]

  cloud_init = templatefile("${path.module}/cloud-init.yaml.tpl", {
    openobserve_tag           = var.openobserve_tag
    keep_tag                  = var.keep_tag
    openobserve_root_email    = var.openobserve_root_email
    openobserve_root_password = var.openobserve_root_password
    spaces_endpoint           = var.spaces_endpoint
    spaces_bucket             = var.spaces_bucket
    spaces_access_key         = var.spaces_access_key
    spaces_secret_key         = var.spaces_secret_key
    keep_webhook_token        = var.keep_webhook_token
    keep_nextauth_secret      = var.keep_nextauth_secret
    cost_env                  = var.cost_env
    # base64 the compose so cloud-init's YAML parser never sees its content
    # (same technique the qa env uses for its compose/Caddyfile).
    compose_obs_b64 = base64encode(file("${path.module}/compose/docker-compose.obs.yml"))
  })
}

# ── Firewall ──────────────────────────────────────────────────────────────
# UIs (5080 OpenObserve, 3034 Keep) + SSH are admin-only (admin_ip_cidr). The
# SEPARATE obs plane also needs CROSS-BOX OTLP ingest on 5080 from off-droplet
# collectors — primarily the agenticos droplet (host.role=agenticos, GOL-54).
# Those source CIDRs go in var.ingest_source_cidrs (a /32 per collector), kept
# distinct from admin_ip_cidr so ingest never widens admin/UI access.
# TODO(live): front OpenObserve ingest with the Cloudflare-WAF Bearer endpoint
# (spec §1) for the off-droplet GitHub Actions Playwright/Hurl crons whose IPs
# are dynamic and can't be pinned to a /32 here.
resource "digitalocean_firewall" "obs" {
  name        = "grove-obs-fw"
  droplet_ids = [module.obs_droplet.droplet_id]

  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = [var.admin_ip_cidr]
  }

  inbound_rule {
    protocol         = "tcp"
    port_range       = "5080" # OpenObserve UI + OTLP ingest (admin)
    source_addresses = [var.admin_ip_cidr]
  }

  # Cross-plane OTLP ingest on 5080 from off-droplet collectors (agenticos
  # droplet, etc.). Only rendered when at least one ingest CIDR is configured.
  dynamic "inbound_rule" {
    for_each = length(var.ingest_source_cidrs) > 0 ? [1] : []
    content {
      protocol         = "tcp"
      port_range       = "5080"
      source_addresses = var.ingest_source_cidrs
    }
  }

  inbound_rule {
    protocol         = "tcp"
    port_range       = "3034" # Keep UI
    source_addresses = [var.admin_ip_cidr]
  }

  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "icmp"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}
