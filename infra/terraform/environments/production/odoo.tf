###############################################################################
# Track 2, step 2 - Odoo droplet + Caddy + durable filestore volume
# (ADR-007 Phase 6, GOL-105).
#
# Copied + sized up from the validated QA L3 env
# (environments/qa-app-platform/main.tf Odoo-droplet block), with ONE
# deliberate divergence: TLS.
#
#   QA L3   : odoo.<qa-zone> in a DO-DELEGATED zone, Caddy issues an LE cert
#             via DNS-01 ACME (grove-caddy image bakes the DO-DNS plugin).
#   Prod    : odoo.gatheringatthegrove.com is a CLOUDFLARE-PROXIED record.
#             Caddy terminates TLS with the Cloudflare Origin CA cert files
#             (the SAME 15-year cert blogs.tf already issues for the hub zone;
#             its `*.gatheringatthegrove.com` SAN covers `odoo.`). No ACME, no
#             rate limits, no DNS plugin - identical to the blogs droplet.
#
# Why proxied: GOL-93's edge-cache rule (environments/cloudflare-policy) caches
# Odoo /web/image/* at the CF edge to keep product-photo traffic off this small
# droplet. That rule is authored-but-inert until this PROXIED record lands -
# this is the record it was waiting on.
#
# The Odoo filestore lives on a durable DO block volume (GOL-93 pattern) that
# survives a droplet replace, so product photos + ir.attachment binaries are
# not wiped on recreate. That volume is the resource GOL-99 wires its nightly
# backup into.
#
# SSH keys + Origin CA certs are declared once in blogs.tf and referenced here
# (data.digitalocean_ssh_key.qa_{deploy,admin}, cloudflare_origin_ca_certificate
# .origin["hub"], tls_private_key.origin["hub"]).
#
# APPLY IS GATED (GOL-105): scaffold/plan now; the prod apply itself waits for
# the QA L3 soak sign-off (~2026-07-21+) and the @CEO final go.
###############################################################################

locals {
  # The Odoo/ERP zone. Its Origin CA cert (blogs.tf) has a wildcard SAN that
  # covers odoo.<apex>; the CF cache rule (GOL-93) is scoped to this apex too.
  odoo_zone = local.tenants["hub"] # gatheringatthegrove.com
  odoo_host = "odoo.${local.odoo_zone}"
}

# ── Durable Odoo filestore volume (GOL-93) ──────────────────────────────────
# /var/lib/odoo: every product photo + all ir.attachment binaries. Distinct
# filesystem label ("filestore") so cloud-init's LABEL= mount is unambiguous.
# Sized up from QA L3's 10 GiB via var.odoo_filestore_volume_size_gb.

resource "digitalocean_volume" "odoo_filestore" {
  region                   = var.region
  name                     = "${var.region}-grove-prod-odoo-filestore"
  size                     = var.odoo_filestore_volume_size_gb
  initial_filesystem_type  = "ext4"
  initial_filesystem_label = "filestore"
  tags                     = concat(local.tags, ["role-odoo"])
  description              = "Durable Odoo filestore (/var/lib/odoo) for the Level 3 prod Odoo droplet. Survives droplet teardown so product photos + ir.attachment binaries are not lost on recreate (GOL-93). GOL-99 wires the nightly backup into this volume."

  # Every product photo / ir.attachment binary lives here, and until
  # GOL-99 lands there is no volume backup — deletion is unrecoverable. (#237)
  lifecycle {
    prevent_destroy = true
  }
}

resource "digitalocean_volume_attachment" "odoo_filestore" {
  droplet_id = digitalocean_droplet.odoo.id
  volume_id  = digitalocean_volume.odoo_filestore.id
}

# ── Odoo droplet ────────────────────────────────────────────────────────────
# Only stateful compute Level 3 keeps on a droplet: Postgres -> Managed PG,
# frontends -> App Platform. s-2vcpu-4gb (double QA L3) for market-season load.

resource "digitalocean_droplet" "odoo" {
  name   = "grove-prod-odoo"
  size   = var.odoo_droplet_size
  image  = var.droplet_image
  region = var.region
  tags   = concat(local.tags, ["role-odoo"])

  ssh_keys = [
    data.digitalocean_ssh_key.qa_deploy.fingerprint,
    data.digitalocean_ssh_key.qa_admin.fingerprint,
  ]

  user_data = templatefile("${path.module}/cloud-init-odoo.yaml.tpl", {
    odoo_zone      = local.odoo_zone
    odoo_image_tag = var.odoo_image_tag
    caddy_tag      = var.caddy_tag

    # Managed PG connection params (private VPC network). odoorc.sh substitutes
    # these into /etc/odoo/odoo.conf at container start.
    pg_host     = digitalocean_database_cluster.pg.private_host
    pg_port     = digitalocean_database_cluster.pg.port
    pg_database = digitalocean_database_db.odoo.name
    pg_user     = digitalocean_database_user.odoo.name
    pg_password = digitalocean_database_user.odoo.password

    # Cloudflare Origin CA cert for the hub zone (wildcard SAN covers odoo.).
    # Reuses the cert blogs.tf already issues - no new cert resource.
    origin_cert = cloudflare_origin_ca_certificate.origin["hub"].certificate
    origin_key  = tls_private_key.origin["hub"].private_key_pem

    compose_yml_b64 = base64encode(file("${path.module}/compose/docker-compose.odoo.yml"))
    caddyfile_b64   = base64encode(file("${path.module}/compose/Caddyfile-odoo.tpl"))
  })

  # Backups/PITR live on Managed PG; the filestore has its own durable volume
  # + GOL-99 backup. DO agent metrics not needed (obs plane covers probes).
  monitoring = false

  # Same delete-timeout bump as every other Grove droplet: DO's API droplet
  # delete can hang past the provider's default 60s context deadline.
  timeouts {
    create = "15m"
    delete = "15m"
  }
}

# ── DNS: odoo.gatheringatthegrove.com (Cloudflare-proxied) ───────────────────
# PROXIED (orange-cloud) so GOL-93's /web/image/* edge cache + the account-wide
# geo-block (environments/cloudflare-policy) apply. CF talks to the origin over
# the Origin CA cert; set the zone SSL mode to Full (strict).

resource "cloudflare_record" "odoo" {
  zone_id = data.cloudflare_zone.brand["hub"].id
  name    = "odoo"
  type    = "A"
  value   = digitalocean_droplet.odoo.ipv4_address
  proxied = true
  ttl     = 1 # 1 = auto (required when proxied)
}

# ── Managed PG trusted-sources firewall ─────────────────────────────────────
# Lock the cluster to the Odoo droplet + the operator CIDR. Defined here (not
# postgres.tf) because it references the droplet, which lands in this step.

resource "digitalocean_database_firewall" "pg" {
  cluster_id = digitalocean_database_cluster.pg.id

  rule {
    type  = "droplet"
    value = digitalocean_droplet.odoo.id
  }

  rule {
    type  = "ip_addr"
    value = split("/", var.admin_ip_cidr)[0]
  }
}

# ── Firewall (Odoo droplet) ─────────────────────────────────────────────────
# 80/443 open to the world: Cloudflare proxies the public traffic. Locking to
# CF IP ranges is the same hardening follow-up deferred for the blogs droplet.

resource "digitalocean_firewall" "odoo" {
  name        = "grove-prod-odoo-fw"
  droplet_ids = [digitalocean_droplet.odoo.id]

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
