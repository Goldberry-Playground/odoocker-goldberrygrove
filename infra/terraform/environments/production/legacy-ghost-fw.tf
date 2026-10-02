###############################################################################
# GOL-2566 - holding-action cloud firewall for the LAST legacy Ghost snowflake
#
# `ghostgoldberrygrove-nyc1` (droplet 468914087, created 2025-01-10) sits in NO
# DO cloud firewall, so every port it listens on answers the whole internet.
# This is NOT Terraform drift: there is no resource for the droplet, so nothing
# can converge, and the nightly membership watcher cannot see it either -- that
# check asserts codified firewalls contain their codified droplets, and an
# un-codified droplet is invisible to it by construction.
#
# WHAT IS ACTUALLY EXPOSED (verified read-only from the agent plane 2026-09-29,
# nothing applied):
#   :22  OPEN   ssh, since 2025-01-10
#   :80  OPEN   301 -> https
#   :443 OPEN   Ghost, serving a Let's Encrypt cert that EXPIRED 2025-10-07
#               (CN=goldberrygrove.farm) -- ~12 months of dead certbot
#   https://178.128.152.218/ghost/            -> 200, Ghost ADMIN LOGIN
#   https://178.128.152.218/ghost/api/admin/site/ -> 200
# So an unattended, unpatched CMS admin login is on the public internet behind a
# year-expired certificate. That, not :22, is the headline.
#
# WHY IT IS SAFE TO FENCE :80/:443 TOO -- the box is DNS-ORPHANED:
#   Across all four Cloudflare zones (goldberrygrove.farm, atthegrovenursery.com,
#   woodworkingeorge.com, gatheringatthegrove.com) exactly ZERO DNS records
#   resolve to 178.128.152.218. Every `blog.*` A record points at 159.89.243.121
#   (the grove-prod-blogs reserved IP) and every apex is a CNAME to an App
#   Platform frontend. No DO load balancer and no reserved IP references it.
#   Nothing routes to this box; it is reachable only by raw IP.
#
# WHY THIS IS A FIREWALL AND NOT A DESTROY (GOL-863):
#   The box holds 29 authored posts (its own sitemap-posts.xml) and it is the
#   SOLE copy. goldberrygrove.farm/blog serves a different, 4-post set from
#   grove-prod-blogs; a 5-slug sample of the legacy archive -- pollarding-vs-
#   coppicing..., november-at-goldberry-grove, the-peoples-nut-that-fed-
#   appalachia, mycoforestry-101..., planting-without-a-mask... -- all return
#   404 on the live site. The archive was never migrated. Destroying this
#   droplet today destroys the farm's entire blog history, so retirement is
#   gated on a content migration, not merely on a CEO go.
#
#   Also correcting GOL-863's premise: its sibling snowflake
#   `gatheratthegrove-blog-nyc` is already gone from the account, so the standing
#   cost is ONE $32/mo droplet plus DO backups (~$6.40/mo), not ~$96/mo.
#
# WHAT THIS DOES NOT DO:
#   It does NOT import or manage the droplet. The droplet is resolved through a
#   `data` source, so Terraform never owns its lifecycle and `terraform destroy`
#   in this env can never delete it -- which matters a lot given what is on it.
#   Only the firewall is managed. Attaching a DO cloud firewall is an in-place
#   change on the firewall object: it does not touch, reboot or replace the
#   droplet, and detaching takes effect immediately.
#
# ROLLBACK: set `legacy_ghost_firewall_enabled = false` and apply, or detach in
# the DO console. Host-level rules on the box are untouched either way. If some
# unknown raw-IP consumer turns out to exist, `legacy_ghost_http_public = true`
# restores today's HTTP reachability without giving up the :22 fence.
###############################################################################

# Resolve the un-codified snowflake by name. If the droplet is ever destroyed
# (the GOL-863 outcome, after the content migration) this data source errors,
# which is the desired signal: flip `legacy_ghost_firewall_enabled` to false in
# the same change that retires the box, and this whole file goes away with it.
data "digitalocean_droplet" "legacy_ghost" {
  count = var.legacy_ghost_firewall_enabled ? 1 : 0
  name  = var.legacy_ghost_droplet_name
}

resource "digitalocean_firewall" "legacy_ghost" {
  count = var.legacy_ghost_firewall_enabled ? 1 : 0

  name        = "grove-legacy-ghost-fw"
  droplet_ids = [data.digitalocean_droplet.legacy_ghost[0].id]

  # SSH goes from 0.0.0.0/0 to the operator CIDRs (var.admin_ip_cidrs, a LIST since GOL-1842 -- appending a
  # new operator address there covers this box too, same as blogs and Odoo).
  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = var.admin_ip_cidrs
  }

  # HTTP/HTTPS -- fenced to the operator by default, because the box is
  # DNS-orphaned (0 records in 4 zones) and what is listening there is a Ghost
  # admin login on an expired cert. Flip `legacy_ghost_http_public` to true to
  # restore world access if an unknown raw-IP consumer surfaces.
  dynamic "inbound_rule" {
    for_each = toset(["80", "443"])
    content {
      protocol   = "tcp"
      port_range = inbound_rule.value
      source_addresses = (
        var.legacy_ghost_http_public
        ? ["0.0.0.0/0", "::/0"]
        : var.admin_ip_cidrs
      )
    }
  }

  # Outbound stays open: the box needs egress for certbot, apt and Ghost's
  # outbound mail. Constricting egress on a box nobody has logged into for
  # months is how a holding action turns into an outage.
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
