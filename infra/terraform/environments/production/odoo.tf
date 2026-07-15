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

  # Every product photo / ir.attachment binary lives here. GOL-99 (below) now
  # mirrors it nightly, so deletion is no longer strictly unrecoverable - but
  # the guard stays: a restore is an incident, and the day-2 "immutable
  # replace" model depends on this volume outliving the droplet. (#237, GOL-382)
  lifecycle {
    prevent_destroy = true
  }
}

resource "digitalocean_volume_attachment" "odoo_filestore" {
  droplet_id = digitalocean_droplet.odoo.id
  volume_id  = digitalocean_volume.odoo_filestore.id
}

# ── Filestore backups: bucket + scoped key (GOL-99 / GOL-382) ───────────────
# The volume above survives a droplet replace. It does NOT survive a volume-level
# failure, a bad migration, or an operator deleting the wrong thing - and
# "the filestore has a durable volume" was being read as "the filestore is
# backed up". It was not: GOL-99 was a comment, not code.
#
# Layout (deliberately NOT the blogs' nightly-tar pattern):
#
#   filestore/current/    live mirror, rclone sync'd nightly
#   filestore/archive/    anything sync deleted or overwrote, per run (35d)
#   filestore/manifest/   one JSON per run: file count + bytes (kept)
#
# Why a mirror instead of a dated tarball: Odoo's filestore is CONTENT-
# ADDRESSED - filestore/<db>/<2-char>/<sha1-of-content>. Files are written
# once and never mutated; only unreferenced ones are GC'd. So an incremental
# sync transfers only genuinely new attachments, where a nightly tar would
# re-upload the entire (50 GiB ceiling) filestore every night and keep 35
# copies of it. That same property is why `archive/` is the safety net a mirror
# normally lacks: sync's only destructive act is deleting, and --backup-dir
# catches every deletion instead of propagating it.
#
# Content-addressing is also what makes filestore-backup + Managed-PG PITR a
# coherent pair rather than two unrelated halves: restoring a filestore from
# time T1 against a DB from a LATER time T2 can only ever be missing files,
# never wrong bytes for a hash. See docs/RUNBOOK-odoo-filestore-restore.md.

resource "digitalocean_spaces_bucket" "odoo_backups" {
  name   = "grove-odoo-backups"
  region = var.region
  acl    = "private"

  lifecycle_rule {
    id      = "expire-archive"
    enabled = true
    prefix  = "filestore/archive/"
    expiration {
      days = 35
    }
  }
  # filestore/current/ + filestore/manifest/ have no rule - a mirror you expire
  # is not a mirror.

  # #237 guarded the volume; this is the other half. Destroying the bucket is
  # the one action that turns a recoverable incident into data loss.
  lifecycle {
    prevent_destroy = true
  }
}

# Bucket-scoped key for the droplet's rclone sync. Same rationale as
# digitalocean_spaces_key.blogs_backup in blogs.tf: the all-buckets plumbing
# key (var.spaces_access_id) is provider auth and must never land on a droplet,
# where user_data is readable at 169.254.169.254 by any process on the box.
# This key can touch ONLY grove-odoo-backups.
resource "digitalocean_spaces_key" "odoo_backup" {
  name = "grove-odoo-backup"

  grant {
    bucket     = digitalocean_spaces_bucket.odoo_backups.name
    permission = "readwrite"
  }
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

    # Nightly filestore backup (GOL-99). Bucket-scoped key, not the plumbing
    # key. Its own Healthchecks check, NOT the blogs one: sharing a check means
    # a green blogs ping masks a dead Odoo backup - the exact failure a
    # dead-man's switch exists to catch.
    spaces_access_id      = digitalocean_spaces_key.odoo_backup.access_key
    spaces_secret_key     = digitalocean_spaces_key.odoo_backup.secret_key
    backups_bucket        = digitalocean_spaces_bucket.odoo_backups.name
    spaces_endpoint       = "https://${var.region}.digitaloceanspaces.com"
    healthchecks_ping_url = var.odoo_backup_healthchecks_ping_url
  })

  # Backups/PITR live on Managed PG; the filestore has its own durable volume
  # + GOL-99 backup.
  #
  # DO metrics agent — REQUIRED by observability.tf's platform-plane alerts
  # (GOL-381). The old "obs plane covers probes" rationale did not survive the
  # GOL-379 audit: the obs plane (grove-obs) has never had a path to this box —
  # no otel-collector in the prod compose and `ingest_source_cidrs = []`. Even
  # once that lands, obs-plane alerting shares a failure domain with a single
  # droplet; this agent is what still pages when grove-obs is the casualty.
  #
  # This droplet is born tagged `env-production`, so the tag-scoped alerts in
  # observability.tf cover it the moment it boots — no follow-up apply.
  monitoring = true

  # Same delete-timeout bump as every other Grove droplet: DO's API droplet
  # delete can hang past the provider's default 60s context deadline.
  timeouts {
    create = "15m"
    delete = "15m"
  }
}

# ── Reserved IP (GOL-382) ───────────────────────────────────────────────────
# The foundation the #242 day-2 model ("immutable droplet replace", ~10-min
# window) assumed but never had. Without it the A record below points at the
# droplet's ephemeral address, so every replace rewrites DNS and the real
# window is bounded by propagation, not by boot - an uncontrolled outage.
#
# Two resources, not one, on purpose: `digitalocean_reserved_ip` also accepts
# an inline `droplet_id`, but that couples the IP's lifecycle to the droplet's.
# A separate assignment resource lets the droplet be destroyed and recreated
# underneath a STABLE address - which is the entire point. The IP is what DNS
# is pinned to, so losing it is the outage we are preventing: prevent_destroy
# guards it (and a released reserved IP is gone for good - DO will not hand
# the same address back).
#
# Note this is the ORIGIN address and the record is CF-proxied, so it is never
# user-visible. It still matters: it is what makes a replace a no-DNS-change op.

resource "digitalocean_reserved_ip" "odoo" {
  region = var.region

  lifecycle {
    prevent_destroy = true
  }
}

# Re-pointed (not recreated) when digitalocean_droplet.odoo is replaced: this
# is the resource that makes step 4's runbook a ~10-min window instead of a
# DNS-propagation gamble.
resource "digitalocean_reserved_ip_assignment" "odoo" {
  ip_address = digitalocean_reserved_ip.odoo.ip_address
  droplet_id = digitalocean_droplet.odoo.id
}

# ── DNS: odoo.gatheringatthegrove.com (Cloudflare-proxied) ───────────────────
# PROXIED (orange-cloud) so GOL-93's /web/image/* edge cache + the account-wide
# geo-block (environments/cloudflare-policy) apply. CF talks to the origin over
# the Origin CA cert; set the zone SSL mode to Full (strict).
#
# Points at the RESERVED IP, never at digitalocean_droplet.odoo.ipv4_address
# (GOL-382). This record must not change when the droplet is replaced.

resource "cloudflare_record" "odoo" {
  zone_id = data.cloudflare_zone.brand["hub"].id
  name    = "odoo"
  type    = "A"
  value   = digitalocean_reserved_ip.odoo.ip_address
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
