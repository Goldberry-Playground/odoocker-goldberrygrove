###############################################################################
# Grove QA — long-lived(ish) QA environment.
#
# Per Josh's dev cycle: local → orbstack → validate → push to `qa` branch →
# auto-deploy to this env → real humans test → push to main → prod + qa teardown.
#
# Lifecycle:
#   `make qa-apply`    — provisions OR updates the QA env in place
#   `make qa-destroy`  — tears down (called by qa-teardown.yml on prod-deploy success)
#
# Cost while running: ~$24/mo (s-2vcpu-4gb in nyc3) + ~$0/mo for Cloudflare/DO DNS.
#
# What this manages (self-contained — no bootstrap-env prerequisite):
#   1. Cloudflare → DO NS delegation for qa.gatheringatthegrove.com
#   2. DO domain for qa.gatheringatthegrove.com + child A/CNAME records
#   3. DO SSH key (operator-generated; public key uploaded)
#   4. DO droplet (Ubuntu 24.04, s-2vcpu-4gb)
#   5. DO firewall (SSH from admin_ip_cidr, 80/443 from anywhere)
#   6. cloud-init: docker install + compose stack bring-up + /grove-ready sentinel
#
# What this does NOT manage:
#   - Ghost containers (frontends point at LIVE blog.goldberrygrove.farm via
#     ghost_key_goldberry, or gracefully degrade when empty)
#   - Persistent volumes (QA is ephemeral — testers create data, it dies
#     on the next qa-destroy + qa-apply cycle. By design.)
#   - Snapshot restore from preview-data Spaces bucket (preview env does
#     that; QA starts empty)
###############################################################################

provider "digitalocean" {
  token = var.do_token
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}

locals {
  qa_zone = "${var.qa_subdomain}.${var.cloudflare_zone_name}"
  tags = [
    "env-qa",
    "project-grove",
  ]
}

data "cloudflare_zone" "apex" {
  name = var.cloudflare_zone_name
}

# ── Cloudflare → DigitalOcean NS delegation for qa.<apex> ───────────────────
# Three NS records under the apex zone in Cloudflare point at DO's nameservers,
# delegating the qa subdomain's DNS management to DO. Same pattern as the
# preview env's bootstrap (PR #19 in odoocker).

resource "cloudflare_record" "qa_ns" {
  for_each = toset([
    "ns1.digitalocean.com",
    "ns2.digitalocean.com",
    "ns3.digitalocean.com",
  ])

  zone_id = data.cloudflare_zone.apex.id
  name    = var.qa_subdomain
  type    = "NS"
  value   = each.value
  ttl     = 1 # 1 = Cloudflare "Auto"
}

# ── DO domain (the delegated zone DO now manages) ───────────────────────────

resource "digitalocean_domain" "qa" {
  name = local.qa_zone
  # No ip_address here — child records (below) handle individual hosts.
}

# ── SSH key (operator-generated via `make qa-keygen`; uploaded here) ────────

resource "digitalocean_ssh_key" "qa_deploy" {
  name       = "grove-qa-deploy"
  public_key = file(pathexpand(var.ssh_public_key_path))
}

# ── Droplet ─────────────────────────────────────────────────────────────────

resource "digitalocean_droplet" "qa" {
  name   = "grove-qa"
  size   = var.droplet_size
  image  = var.droplet_image
  region = var.region
  tags   = local.tags

  ssh_keys = [digitalocean_ssh_key.qa_deploy.fingerprint]

  # Self-bootstrapping via cloud-init. See cloud-init.yaml.tpl for the
  # script — it installs docker, writes /etc/grove/{.env,docker-compose.yml,
  # Caddyfile}, brings the stack up, and touches /var/lib/cloud/instance/
  # grove-ready when the stack is responding on https://localhost/.
  user_data = templatefile("${path.module}/cloud-init.yaml.tpl", {
    qa_zone             = local.qa_zone
    do_token_for_caddy  = var.do_token
    odoo_image_tag      = var.odoo_image_tag
    frontend_image_tags = var.frontend_image_tags
    ghost_key_goldberry = var.ghost_key_goldberry
    compose_yml         = file("${path.module}/compose/docker-compose.qa.yml")
    caddyfile_tpl       = file("${path.module}/compose/Caddyfile.tpl")
  })

  monitoring = false
}

# ── DNS records inside the delegated qa zone ────────────────────────────────

# Apex A record — qa.gatheringatthegrove.com → droplet IP. The hub frontend
# is served here (no subdomain prefix).
resource "digitalocean_record" "qa_apex" {
  domain = digitalocean_domain.qa.name
  type   = "A"
  name   = "@" # the apex of the delegated qa zone
  value  = digitalocean_droplet.qa.ipv4_address
  ttl    = 300
}

# Per-tenant CNAMEs — qa-goldberry.qa.gatheringatthegrove.com, etc.
# Caddy on the droplet routes by Host header to the right frontend container.
#
# Naming choice: we use child labels (goldberry → goldberry.qa.gatheringatthegrove.com)
# instead of a flat `qa-goldberry.gatheringatthegrove.com` prefix, because the
# delegation gives DO control over the entire qa subdomain. Cleaner.
resource "digitalocean_record" "tenant" {
  for_each = toset(var.tenant_subdomains)

  domain = digitalocean_domain.qa.name
  type   = "CNAME"
  name   = each.key
  value  = "@" # FQDN within the qa zone — resolves to qa.gatheringatthegrove.com
  ttl    = 300
}

# ── Firewall ────────────────────────────────────────────────────────────────

resource "digitalocean_firewall" "qa" {
  name        = "grove-qa-fw"
  droplet_ids = [digitalocean_droplet.qa.id]

  # SSH — scoped to the operator
  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = [var.admin_ip_cidr]
  }

  # HTTP — Caddy listens here for ACME HTTP-01 fallback + redirects to 443
  inbound_rule {
    protocol         = "tcp"
    port_range       = "80"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  # HTTPS — public surface
  inbound_rule {
    protocol         = "tcp"
    port_range       = "443"
    source_addresses = ["0.0.0.0/0", "::/0"]
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
