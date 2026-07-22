###############################################################################
# Track 1 - Blogs droplet (Grove Production Launch spec 2026-07-04).
#
# Replaces the two hand-managed Ghost snowflakes. 4x Ghost 6 + MySQL 8 +
# Caddy; all state on a persistent volume; TLS via CF Origin CA (the four
# brand zones are Cloudflare-proxied, so the origin only ever talks to CF's
# edge - Origin CA certs are free, 15-year, and Terraform-issued: no ACME).
###############################################################################

# -- Origin CA certs (one per brand zone, covers apex + wildcard) -------------

resource "tls_private_key" "origin" {
  for_each  = local.tenants
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_cert_request" "origin" {
  for_each        = local.tenants
  private_key_pem = tls_private_key.origin[each.key].private_key_pem

  subject {
    common_name  = each.value
    organization = "Gathering at the Grove"
  }
}

resource "cloudflare_origin_ca_certificate" "origin" {
  for_each           = local.tenants
  csr                = tls_cert_request.origin[each.key].cert_request_pem
  hostnames          = [each.value, "*.${each.value}"]
  request_type       = "origin-rsa"
  requested_validity = 5475 # 15 years
}

# -- Backups bucket ------------------------------------------------------------

resource "digitalocean_spaces_bucket" "blogs_backups" {
  name   = "grove-blogs-backups"
  region = var.region
  acl    = "private"

  lifecycle_rule {
    id      = "expire-dailies"
    enabled = true
    prefix  = "daily/"
    expiration {
      days = 35
    }
  }
  # monthly/ prefix has no rule - kept indefinitely.

  # #237 guarded blogs_data (the volume); this is the other half of the same
  # argument. The volume guard assumes "the nightly Spaces backup covers
  # data-level loss" - that assumption only holds if the bucket holding those
  # backups cannot itself be destroyed. (GOL-382)
  lifecycle {
    prevent_destroy = true
  }
}

# Scoped Spaces key for the droplet's rclone backups. The all-buckets
# "plumbing" key (var.spaces_access_id) stays TF-side only: it is provider
# auth for bucket ops and must never land on the droplet, where the
# metadata service exposes user_data to any process on the box. This key
# can touch ONLY the backups bucket.
resource "digitalocean_spaces_key" "blogs_backup" {
  name = "grove-blogs-backup"

  grant {
    bucket     = digitalocean_spaces_bucket.blogs_backups.name
    permission = "readwrite"
  }
}

# -- Persistent data volume ------------------------------------------------------

resource "digitalocean_volume" "blogs_data" {
  region                   = var.region
  name                     = "${var.region}-grove-prod-blogs-data"
  size                     = 20
  initial_filesystem_type  = "ext4"
  initial_filesystem_label = "blogsdata"
  tags                     = local.tags
  description              = "All blog state: MySQL data dir + 4 Ghost content dirs. Survives droplet replacement."

  # Live blog content for all four brands (MySQL + Ghost content dirs).
  # The nightly Spaces backup covers data-level loss, but volume deletion
  # should still require deliberately removing this guard. (#237)
  lifecycle {
    prevent_destroy = true
  }
}

resource "digitalocean_volume_attachment" "blogs_data" {
  droplet_id = digitalocean_droplet.blogs.id
  volume_id  = digitalocean_volume.blogs_data.id
}

# -- SSH keys (same shared-key pattern as qa-l3; see qa-app-platform/main.tf) --

data "digitalocean_ssh_key" "qa_deploy" {
  name = "grove-qa-deploy"
}

data "digitalocean_ssh_key" "qa_admin" {
  name = "grove-qa-admin"
}

# -- The droplet ---------------------------------------------------------------

resource "digitalocean_droplet" "blogs" {
  name   = "grove-prod-blogs"
  size   = var.blogs_droplet_size
  image  = var.droplet_image
  region = var.region
  tags   = concat(local.tags, ["role-blogs"])

  ssh_keys = [
    data.digitalocean_ssh_key.qa_deploy.fingerprint,
    data.digitalocean_ssh_key.qa_admin.fingerprint,
  ]

  user_data = templatefile("${path.module}/cloud-init-blogs.yaml.tpl", {
    ghost_tag = var.ghost_tag
    mysql_tag = var.mysql_tag
    caddy_tag = var.caddy_tag

    # URL policy (EOM-July QA cutover, 2026-07-20): ALL four blogs are
    # canonical at blog.* -- hub + goldberry flipped from their apexes so
    # the apexes can 302 to blog.* via Cloudflare Redirect Rules (302 now,
    # 301 at the prod launch cutover). Reader-visible sequencing lives in
    # qa-tools/ghost-blog-migration-commands.md (gather-at-the-grove repo):
    # blog.* DNS already points here (GOL-387 Phase 1), THEN this applies,
    # THEN the CF redirect rules are enabled.
    #
    # WARNING: this edits user_data => digitalocean_droplet.blogs is
    # REPLACED on apply (ForceNew). That replace was already pending
    # (user_data drift + monitoring flip) and is deliberately held --
    # see docs/RUNBOOK-blogs-reserved-ip-cutover.md "Phase 2". Applying
    # this change IS running Phase 2: snapshot + backup check first,
    # named window, 10-20 min blogs outage, volume + reserved IP survive.
    ghost_urls = {
      hub       = "https://blog.gatheringatthegrove.com"
      goldberry = "https://blog.goldberrygrove.farm"
      ggg       = "https://blog.woodworkingeorge.com"
      nursery   = "https://blog.atthegrovenursery.com"
    }

    # Admin lives on blog.* from day one. Now redundant with ghost_urls
    # (canonical url is blog.* for all four since the EOM-July flip) but
    # kept: an explicit admin__url is harmless and removing it would be a
    # second gratuitous user_data change.
    ghost_admin_urls = {
      hub       = "https://blog.gatheringatthegrove.com"
      goldberry = "https://blog.goldberrygrove.farm"
    }

    volume_name = digitalocean_volume.blogs_data.name

    # Transactional email - Mailgun SMTP (GOL-248). Per-tenant creds land at
    # cutover via TF_VAR_ghost_smtp; empty stub => inert transport, no regression.
    ghost_smtp_host                 = var.ghost_smtp_host
    ghost_smtp_port                 = var.ghost_smtp_port
    ghost_staff_device_verification = var.ghost_staff_device_verification
    ghost_smtp                      = var.ghost_smtp

    origin_certs = {
      for k, z in local.tenants : z => {
        cert = cloudflare_origin_ca_certificate.origin[k].certificate
        key  = tls_private_key.origin[k].private_key_pem
      }
    }

    compose_yml_b64 = base64encode(file("${path.module}/compose/docker-compose.blogs.yml"))
    caddyfile_b64   = base64encode(file("${path.module}/compose/Caddyfile-blogs.tpl"))
    mysql_init_b64  = base64encode(file("${path.module}/compose/mysql-init.sql.tpl"))

    spaces_access_id      = digitalocean_spaces_key.blogs_backup.access_key
    spaces_secret_key     = digitalocean_spaces_key.blogs_backup.secret_key
    backups_bucket        = digitalocean_spaces_bucket.blogs_backups.name
    spaces_endpoint       = "https://${var.region}.digitaloceanspaces.com"
    healthchecks_ping_url = var.healthchecks_ping_url
  })

  # DO metrics agent — REQUIRED by the platform-plane alerts in observability.tf
  # (GOL-381). `v1/insights/droplet/*` alerts read the agent's stream, not the
  # hypervisor: with the agent absent those alerts never fire and report green
  # forever, which is worse than having no alert at all.
  #
  # Supersedes the prior "not needed: Healthchecks covers backups, and synthetic
  # probes cover the public surface" rationale. That was wrong on both counts —
  # Healthchecks only covers the nightly backup's liveness, and the synthetic
  # probes it deferred to were never wired to prod (GOL-379 audit). Neither one
  # sees CPU/RAM/disk on this box.
  monitoring = true

  timeouts {
    create = "15m"
    delete = "15m"
  }
}

# -- Reserved IP ---------------------------------------------------------------
# Same shape as the Odoo reserved IP (GOL-382), and for the same reason: DNS
# must be pinned to an address the droplet can be replaced underneath.
#
# Two resources, not one, on purpose: `digitalocean_reserved_ip` also accepts an
# inline `droplet_id`, but that couples the IP's lifecycle to the droplet's and
# defeats the point. A separate assignment resource re-points (rather than
# recreates) when digitalocean_droplet.blogs is replaced.
#
# This droplet is LIVE and serves all four brand blogs, so unlike the Odoo one
# this IP is retrofitted under running traffic. See the sequencing note on
# reserved_ip_assignment below - the order is not optional. (GOL-387)

resource "digitalocean_reserved_ip" "blogs" {
  region = var.region

  # A released reserved IP is gone for good - DO will not hand the same address
  # back, and this one is what four zones' DNS is pinned to. (GOL-382)
  lifecycle {
    prevent_destroy = true
  }
}

# SEQUENCING (GOL-387) - this resource cannot be reached with `-target` without
# also replacing the live droplet, because `-target` drags in the target's
# dependencies and digitalocean_droplet.blogs currently has a pending replace
# (both `user_data` and `monitoring` are ForceNew and both differ from what is
# applied). Targeting the assignment would therefore eat the exact outage this
# resource exists to prevent.
#
# So the first assignment is made out-of-band against the RUNNING droplet and
# imported, which is a no-op in state and needs no target. Only then is DNS
# repointed; only then, in a chosen window, is the droplet replaced. The full
# ordered procedure lives in docs/RUNBOOK-blogs-reserved-ip-cutover.md.
resource "digitalocean_reserved_ip_assignment" "blogs" {
  ip_address = digitalocean_reserved_ip.blogs.ip_address
  droplet_id = digitalocean_droplet.blogs.id
}

# -- blog.* DNS records (all four brand zones, Cloudflare-proxied) --------------
# Points at the RESERVED IP, never at digitalocean_droplet.blogs.ipv4_address
# (GOL-387). Before this, a droplet replace dragged all four records to
# "(known after apply)" - a TF apply plus DNS propagation in the middle of an
# outage. Verified with `terraform graph`: no edge from these records to the
# droplet.
#
# The records are CF-proxied, so the origin address is never user-visible; the
# value here only decides where Cloudflare's edge sends traffic.

resource "cloudflare_record" "blog" {
  for_each = local.tenants

  zone_id = data.cloudflare_zone.brand[each.key].id
  name    = "blog"
  type    = "A"
  value   = digitalocean_reserved_ip.blogs.ip_address
  proxied = true
  ttl     = 1 # 1 = auto (required when proxied)
}

# -- Firewall --------------------------------------------------------------------
# 80/443 open to the world: Cloudflare proxies the public traffic, and
# locking to CF IP ranges is a hardening follow-up (needs the published
# CF ranges kept fresh - deferred; noted in the launch spec's automation
# backlog).

resource "digitalocean_firewall" "blogs" {
  name        = "grove-prod-blogs-fw"
  droplet_ids = [digitalocean_droplet.blogs.id]

  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = [var.admin_ip_cidr]
  }

  inbound_rule {
    protocol         = "tcp"
    port_range       = "80"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

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
