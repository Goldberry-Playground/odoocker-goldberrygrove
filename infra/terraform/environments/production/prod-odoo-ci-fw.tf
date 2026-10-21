###############################################################################
# GOL-3414 - the CI-OWNED sibling firewall on grove-prod-odoo
#
# `grove-prod-odoo-fw` (odoo.tf, GOL-2565, re-attached 2026-09-29) admits tcp/22
# on the prod Odoo droplet ONLY from var.admin_ip_cidrs. That is the intended
# steady state. It also cuts off one inbound consumer the fence never listed:
# three workflow_dispatch provisioning workflows (provision-project-agents,
# provision-logistics-otto, provision-ceo-odoo-login) SSH in as
# `root@$PROD_HOST` from GitHub-HOSTED runners, which egress from ~7,000
# rotating Azure CIDRs. No static source list can admit them, and an ephemeral
# rule on `grove-prod-odoo-fw` itself would plan as drift on every run and be
# reverted by any apply that lands mid-job.
#
# Same answer as the agent plane (GOL-3412, agent-plane-fw.tf
# `digitalocean_firewall.agent_plane_ci`): a SECOND cloud firewall on the SAME
# droplet. Each prod dispatch adds its runner's /32 on tcp/22 to THIS firewall
# before the keyscan and removes it in an `if: always()` step
# (AgenticOS `.github/actions/agent-plane-fence`, pinned by SHA in the three
# workflows; runbook docs/RUNBOOK-prod-odoo-ci-fence-GOL-3414.md).
#
# UNION SEMANTICS. DigitalOcean evaluates every firewall attached to a droplet
# and admits a packet if ANY of them does -- membership is additive, never
# intersecting. So the fence in odoo.tf stays exactly as codified (operator
# /32s only) and this firewall contributes its transient runner /32s on top.
# Nothing here widens the fence: with no inbound rules it admits nothing by
# itself, and a /32 a killed runner leaves behind is confined to a firewall
# whose name says who owns it (the admit step warns once 5+ stray rules
# accumulate). :80/:443 are already world-open on the fence; this firewall
# never carries them.
#
# WHO OWNS WHAT:
#   Terraform (this file)  existence, name, droplet membership, outbound rules.
#                          The nightly membership watcher
#                          (scripts/check-firewall-membership.py) asserts the
#                          droplet is behind it like any other codified
#                          firewall once it exists.
#   odoocker CI            every inbound_rule, all of them transient. Hence
#                          `ignore_changes = [inbound_rule]`: a plan taken while
#                          a job is admitted must not show its /32 as drift, and
#                          an apply must never strip one out from under a running
#                          provisioning job. This resource declares NO
#                          inbound_rule blocks on purpose. Do not add one -- a
#                          permanent admission belongs on `grove-prod-odoo-fw`
#                          in odoo.tf, where it is reviewed.
#
# OUTBOUND is the same three wide-open rules as the fence, for the same reason:
# a DO firewall with no outbound rules blocks ALL egress, and this one must
# never be the firewall that takes production Odoo offline.
#
# LIFECYCLE / ORDERING. Unlike the agent-plane sibling, this firewall does NOT
# exist live yet (the three workflows have never been dispatched), so there is
# no `import` block: the first apply after merge creates it (`1 to add`). The
# fence action also creates the firewall itself if a prod dispatch runs BEFORE
# that apply. If `doctl compute firewall list` already shows
# `grove-prod-odoo-ci-fw` when you go to apply, do NOT let apply create a
# second one with the same name -- add an `import` block with the live id
# (the pattern agent-plane-fw.tf uses) so Terraform adopts it instead.
#
# Count-gated so the membership watcher treats "declared but not live yet" as
# expected rather than drift between merge and apply (it SKIPs count-gated
# firewalls that are absent). Defaults TRUE: a plain prod apply converges on
# it. Flipping it false DETACHES and destroys the firewall (count -> 0) and
# every prod provisioning dispatch goes back to timing out in the keyscan.
###############################################################################

variable "prod_odoo_ci_firewall_enabled" {
  description = "Attach `grove-prod-odoo-ci-fw`, the CI-owned sibling firewall that admits one GitHub-runner /32 on tcp/22 per provisioning job next to the operator-only fence `grove-prod-odoo-fw` (GOL-3414). TRUE by default so a plain apply creates/converges it. Set false only as the documented rollback in docs/RUNBOOK-prod-odoo-ci-fence-GOL-3414.md -- the three provision-*.yml workflows then fail in their keyscan on every prod dispatch."
  type        = bool
  default     = true
}

resource "digitalocean_firewall" "odoo_ci" {
  count = var.prod_odoo_ci_firewall_enabled ? 1 : 0

  name        = "grove-prod-odoo-ci-fw"
  droplet_ids = [digitalocean_droplet.odoo.id]

  # Deliberately NO inbound_rule blocks. CI adds and removes its own per job;
  # see the header and the lifecycle block below.

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

  lifecycle {
    # The inbound rules are CI's, and they come and go per job. Without this,
    # every plan taken during a provisioning run shows a /32 to remove, and an
    # apply at the wrong moment cuts a running job's SSH. Terraform still owns
    # everything else on the object.
    ignore_changes = [inbound_rule]
  }
}
