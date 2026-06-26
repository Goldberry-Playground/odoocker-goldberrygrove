###############################################################################
# !! DO NOT DEPLOY THIS ENVIRONMENT YET !!
#
# Production deployment is deferred pending the Level 3 architectural rethink
# documented in docs/ADR/007-level-3-app-platform-migration.md.
#
# Decided 2026-06-26: production will be built on DO App Platform + DO Managed
# Postgres + a tiny Odoo droplet, NOT on the current monolithic-droplet shape
# that this TF env scaffolds. The current resources here (modules/droplet calls
# for app + monitoring) reflect the PRE-Level-3 design and will be substantially
# rewritten when Phase 6 of ADR-007 ships.
#
# Why this banner exists:
#   - This env's main.tf is mostly valid TF and `terraform apply` would
#     half-provision the OLD architecture (droplet + volumes + firewalls)
#   - Tonight's QA work (PRs #95-#103) added cert resilience layers (persistent
#     Caddy /data, multi-issuer fallback, orphan TXT cleanup) that are scoped
#     to QA's Caddy+DNS-01 stack. Production-as-currently-designed uses nginx +
#     manual ACME and would NOT inherit those resilience layers
#   - The right move is to NOT deploy this env. Wait for Level 3.
#
# When IS it OK to deploy this env?
#   - After Phase 6 of ADR-007 ships (timeline ~2-4 weeks after Level 3 QA
#     validates on App Platform)
#   - Phase 6 will rewrite this env to use App Platform + Managed PG
#   - This banner should be removed in that same PR
#
# If you absolutely need a prod-shaped environment NOW (e.g., for staging or
# a customer demo), discuss with Josh first. Don't `terraform apply` this env
# without explicit go-ahead.
#
# Cross-references:
#   - docs/ADR/005-qa-cert-resilience-stack.md (the QA work that exposed the gap)
#   - docs/ADR/006-hub-qa-subdomain-not-apex.md (URL convention divergence)
#   - docs/ADR/007-level-3-app-platform-migration.md (the resolution)
#   - infra/terraform/environments/production/README.md (operator playbook)
###############################################################################

locals {
  app_tags = [
    "env-production",
    "project-grove",
    "role-app",
  ]

  monitoring_tags = [
    "env-production",
    "project-grove",
    "role-monitoring",
  ]

  # The 7 public hostnames from nginx/grove-ghost.conf and docs/DEPLOYMENT.md.
  # Key = subdomain label for the A record; value = the full hostname.
  # Records point the app Droplet IP at the time of apply.
  dns_records = {
    # Odoo ERP (catch-all via nginx server_name _)
    "erp.gatheringatthegrove.com" = { domain = "gatheringatthegrove.com", subdomain = "erp" }

    # Ghost CMS blogs
    "blog.goldberrygrove.farm"   = { domain = "goldberrygrove.farm", subdomain = "blog" }
    "blog.woodworkingeorge.com"  = { domain = "woodworkingeorge.com", subdomain = "blog" }
    "blog.atthegrovenursery.com" = { domain = "atthegrovenursery.com", subdomain = "blog" }

    # Apex domains (naked — served by nginx-proxy)
    "goldberrygrove.farm"   = { domain = "goldberrygrove.farm", subdomain = "@" }
    "woodworkingeorge.com"  = { domain = "woodworkingeorge.com", subdomain = "@" }
    "atthegrovenursery.com" = { domain = "atthegrovenursery.com", subdomain = "@" }
  }
}

# ── App Droplet ───────────────────────────────────────────────────────────────
module "app" {
  source = "../../modules/droplet"

  name           = "grove-production-app"
  size           = "s-4vcpu-8gb"
  region         = var.region
  image          = "ubuntu-24-04-x64"
  ssh_key_ids    = var.ssh_key_ids
  volume_size_gb = 100
  tags           = local.app_tags
  monitoring     = true

  # No cloud_init for production — bootstrapped manually via docs/DEPLOY.md
  # so operators can review before first run.
  cloud_init = ""
}

# ── Monitoring Droplet (separate failure domain) ──────────────────────────────
module "monitoring" {
  source = "../../modules/droplet"

  name           = "grove-production-monitoring"
  size           = "s-2vcpu-4gb"
  region         = var.region
  image          = "ubuntu-24-04-x64"
  ssh_key_ids    = var.ssh_key_ids
  volume_size_gb = 20
  tags           = local.monitoring_tags
  monitoring     = true
  cloud_init     = ""
}

# ── Firewall ─────────────────────────────────────────────────────────────────
resource "digitalocean_firewall" "app" {
  name = "grove-production-app"

  droplet_ids = [module.app.droplet_id]

  # HTTP — open to the world (nginx-proxy handles TLS)
  inbound_rule {
    protocol         = "tcp"
    port_range       = "80"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  # HTTPS — open to the world
  inbound_rule {
    protocol         = "tcp"
    port_range       = "443"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  # SSH — restricted to admin CIDR
  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = [var.admin_cidr]
  }

  # Allow all outbound (package updates, Let's Encrypt, git clone)
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

resource "digitalocean_firewall" "monitoring" {
  name = "grove-production-monitoring"

  droplet_ids = [module.monitoring.droplet_id]

  # SSH — restricted to admin CIDR
  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = [var.admin_cidr]
  }

  # Grafana / monitoring dashboards — restrict to admin CIDR
  inbound_rule {
    protocol         = "tcp"
    port_range       = "3000"
    source_addresses = [var.admin_cidr]
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

# ── DNS A Records ─────────────────────────────────────────────────────────────
# DNS is managed in Cloudflare, not DigitalOcean.
# The following A records must exist in CF, pointing at the production Droplet's ipv4_address (output `production_droplet_ip`):
#   - gatheringatthegrove.com         (and www)
#   - goldberrygrove.farm             (and www)
#   - woodworkingeorge.com            (and www)
#   - atthegrovenursery.com           (and www)
#   - erp.gatheringatthegrove.com
#   - blog.goldberrygrove.farm
#   - blog.woodworkingeorge.com
#   - blog.atthegrovenursery.com
# Plus monitoring on the monitoring Droplet (output `monitoring_droplet_ip`):
#   - grafana.gatheringatthegrove.com
#   - status.gatheringatthegrove.com
