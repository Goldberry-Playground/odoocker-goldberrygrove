###############################################################################
# GOL-2566 / GOL-863 - cloud firewall that USED to fence the last legacy Ghost
# snowflake, `ghostgoldberrygrove-nyc1` (droplet 468914087, 2025-01-10).
#
# STATUS: RETIRING. The board accepted the destroy of that droplet on
# 2026-11-06 (GOL-863 confirmation card). The content archive it held (28 posts
# + 4 pages) is live on grove-prod-blogs / goldberrygrove.farm/blog since
# Train #4 (2026-11-05); DR = DO snapshots 251634997 (2026-10-26) and
# 241457688 (2026-08-16), both of which outlive the droplet. History of why the
# fence existed (internet-wide :22/:443 on a Ghost admin login behind an
# expired cert) is in `git log -p` of this file and in
# docs/RUNBOOK-legacy-ghost-firewall.md.
#
# WHY THIS FILE STILL EXISTS, EMPTY, INSTEAD OF BEING DELETED IN ONE STEP:
#   1. The droplet used to be resolved by NAME through `data
#      "digitalocean_droplet"`. The moment the droplet is destroyed that lookup
#      ERRORS on every production plan -- and `prod-plan-guard` re-runs the plan
#      for every merge-group entry that touches production terraform. So the
#      data source has to be gone from main BEFORE the droplet is.
#   2. Deleting the firewall resource (or flipping its count to 0) in the same
#      PR would be a DESTROY of a live resource, which `prod-plan-guard` fails by
#      design. Destroys of live prod resources are taken deliberately in a
#      board-gated apply, never landed via a PR.
#   Hence the two-step retirement:
#     step 1 (this change)  drop the data source, pin `droplet_ids = []`. On a
#                           still-live droplet the plan is ONE in-place update
#                           (droplet_ids [468914087] -> []); once the droplet is
#                           destroyed DO has already emptied the list and the
#                           plan is clean either way. Never a destroy/replace.
#     step 2 (GOL-863 follow-up, board-gated)
#                           `op run --env-file=.env.op -- terraform apply
#                            -var legacy_ghost_firewall_enabled=false`
#                           destroys the now-empty firewall (4351b197), then a
#                           PR deletes this file + its 2 variables; that PR's
#                           plan is clean because the resource is already gone.
#
# Rollback of step 1 is just reverting the commit: re-attaching a droplet to a
# DO cloud firewall is an in-place change on the firewall object.
###############################################################################

resource "digitalocean_firewall" "legacy_ghost" {
  count = var.legacy_ghost_firewall_enabled ? 1 : 0

  name = "grove-legacy-ghost-fw"
  # Empty on purpose (step 1 above): the droplet this fence was for is being
  # destroyed under GOL-863. DO drops a destroyed droplet from every firewall
  # it was attached to, so [] is both the desired and the eventual live state.
  droplet_ids = []

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

  # Rules kept byte-identical to the applied fence so step 1 is a single
  # droplet_ids update and nothing else; they attach to nothing now.
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
