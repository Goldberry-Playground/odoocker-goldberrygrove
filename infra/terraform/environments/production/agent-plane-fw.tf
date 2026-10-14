###############################################################################
# GOL-2569 - holding-action cloud firewall for the AGENT-PLANE snowflake
#
# `agenticos-droplet` (droplet 572389418, 159.223.171.231, created 2026-05-21,
# tags ["agenticos","Production"]) sits in NO DO cloud firewall. Same structural
# blind spot as the legacy Ghost box next door in legacy-ghost-fw.tf: there is no
# Terraform resource for the droplet, so nothing can converge, and the nightly
# membership watcher cannot see it either -- that check asserts codified
# firewalls contain their codified droplets, and an un-codified droplet is
# invisible to it by construction.
#
# WHY THIS ONE MATTERS MOST -- what is on the box (GOL-2306):
#   ~/.config/doctl/config.yaml  an account-wide DigitalOcean API token, NOT
#                                read-scoped. It can mutate or delete any
#                                firewall, droplet or Space on the account --
#                                including the two firewalls GOL-2565/GOL-2566
#                                exist to attach.
#   grove_qa_admin               Odoo credentials.
#   /paperclip/gh-broker.key     + the Paperclip API key.
#   AND a standing SSH vantage: grove-obs-fw admits 159.223.171.231/32 on :22
#                                and on :5080 (OTLP ingest). So this box is
#                                already a codified jump host INTO observability.
#   An unfiltered box holding all of that is a privilege-escalation path into
#   everything else. GOL-2306 covers rescoping the token; this file is the
#   network-side half it does not cover.
#
# WHAT IS ACTUALLY EXPOSED (measured 2026-09-29, read-only, nothing applied).
# The audit that filed this issue could not answer this: the agent runs ON this
# droplet, so a probe of 159.223.171.231 from the agent is a self-probe. It IS
# answerable from here after all, because the agent runs in a CONTAINER in its
# own netns (172.18.0.10) -- so the host's 0.0.0.0-bound listeners can be
# enumerated by scanning the docker bridge gateway 172.18.0.1, which is a
# different address than the host's own loopback and therefore does not answer
# 127.0.0.1-only binds. Ports 1-10000 plus a service list (2375/2376, 3100,
# 5432, 6379, 8069, 9090, 22000, 25565, 27017, ...):
#
#   tcp/22    OPEN   sshd. Also OPEN on the public IP.
#   tcp/8384  OPEN   Syncthing v1.30.0 GUI/REST (`X-Syncthing-Version: v1.30.0`,
#                    device id WVZZVLH-...). Also OPEN on the public IP, i.e.
#                    bound 0.0.0.0, not localhost. Its REST API DOES require
#                    auth (`GET /rest/system/status` -> 403 Forbidden
#                    unauthenticated), so this is not an open control plane --
#                    but it is an authenticating admin endpoint plus a version
#                    banner, on the internet, on the box that holds the DO
#                    token. It has no business being world-reachable.
#   everything else in that range: closed.
#
#   tcp/22000 (Syncthing sync) and tcp/21027 are NOT listening -- sync runs
#   outbound via relays. So fencing inbound does not stop Syncthing syncing;
#   see agent_plane_syncthing_sync_public for the one-flag rollback if it does.
#
#   There is NO inbound HTTP listener at all (no :80, :443, :3100). The Paperclip
#   dashboard's public URL therefore cannot be served by an inbound port -- it is
#   an outbound tunnel, and outbound is left wide open below, so an inbound fence
#   cannot break the dashboard. That was the single biggest self-inflicted-outage
#   risk in this change and it is measured, not assumed.
#
#   Caveat kept honest: the public-IP legs of that scan are host-local hairpins
#   (agent container -> the droplet's own public IP), which never traverse a DO
#   cloud firewall. They prove the BIND address is 0.0.0.0, not the absence of an
#   upstream filter. With no cloud firewall attached, DO filters nothing, so
#   0.0.0.0 + no firewall = world-reachable. A truly external confirmation still
#   wants one `nc -vz 159.223.171.231 22 8384` from off-host (runbook step 0).
#
# WHY A FIREWALL AND NOT A HOST-LEVEL RULE:
#   The box is a Docker host. Docker inserts its own iptables DNAT/FORWARD rules
#   that BYPASS ufw, so a published container port is world-open regardless of
#   what ufw says. A cloud firewall sits in front of the NIC and is the only
#   fence Docker cannot punch through. (It is also the only one an agent on the
#   box cannot silently disable.)
#
# WHAT THIS DOES NOT DO:
#   It does NOT import or manage the droplet -- the droplet is resolved through a
#   `data` source, so Terraform never owns its lifecycle and `terraform destroy`
#   in this env can never delete the box the agents run on. Only the firewall is
#   managed. Attaching a DO cloud firewall is an in-place change on the firewall
#   object: it does not touch, reboot or replace the droplet, and detaching takes
#   effect immediately.
#
# THE ONE REAL HAZARD -- self-inflicted lockout:
#   :22 is fenced to var.admin_ip_cidrs (173.84.140.152/32, 74.47.41.38/32). If
#   Josh's ISP has rotated his address since those were codified, the apply takes
#   his SSH away from the box that runs every agent. It is recoverable (the DO
#   web Recovery Console is out-of-band and unaffected by cloud firewalls), but
#   the runbook's step 1 is "confirm your current egress IP is in that list
#   BEFORE applying", and step 5 is "prove your own SSH still works before
#   walking away" (GOL-1842).
#
# ROLLBACK: set `agent_plane_firewall_enabled = false` and apply, or detach in
# the DO console (immediate). Host-level rules are untouched either way.
#
# Related: GOL-2565 (prod Odoo membership drift - APPLIED, verified filtered
# 2026-09-29), GOL-2566 (legacy Ghost snowflake, legacy-ghost-fw.tf), GOL-2306
# (the token half), PR #745 (membership watcher that cannot cover un-codified
# droplets - see GOL-2572 for the census extension that can).
###############################################################################

# Resolve the un-codified snowflake by name rather than pinning the bare id
# 572389418: the DO `droplet` data source has no id lookup, and the name is the
# stable handle a rebuild reuses. Gated by the same flag as the firewall so a
# plan with the feature off makes no DO API call for it at all.
data "digitalocean_droplet" "agent_plane" {
  count = var.agent_plane_firewall_enabled ? 1 : 0
  name  = var.agent_plane_droplet_name
}

resource "digitalocean_firewall" "agent_plane" {
  count = var.agent_plane_firewall_enabled ? 1 : 0

  name        = "grove-agent-plane-fw"
  droplet_ids = [data.digitalocean_droplet.agent_plane[0].id]

  # SSH: 0.0.0.0/0 -> the operator CIDRs. var.admin_ip_cidrs is a LIST since
  # GOL-1842, so appending a new operator address there covers this box too,
  # exactly like blogs, Odoo and the legacy Ghost box.
  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = var.admin_ip_cidrs
  }

  # Syncthing GUI/REST on :8384 -- operator-only. Measured bound to 0.0.0.0 and
  # answering on the public IP today. Auth is on (unauthenticated REST returns
  # 403), so this is defence in depth over a working lock, not a patch for an
  # open door. The durable fix is to rebind it to 127.0.0.1 in Syncthing's own
  # config (or put it behind Cloudflare Access) -- that is host config, not
  # Terraform, and it is tracked as a follow-up rather than blocking this fence.
  inbound_rule {
    protocol         = "tcp"
    port_range       = "8384"
    source_addresses = var.admin_ip_cidrs
  }

  # Syncthing SYNC transport -- OFF by default because it is not listening:
  # tcp/22000 was closed from both the bridge gateway and the public IP on
  # 2026-09-29, so peers are reached outbound via relays and no inbound rule is
  # needed. Flip this to true if sync degrades after the apply; Syncthing's
  # protocol is device-id-authenticated and TLS-encrypted, so world-source is
  # its normal posture.
  dynamic "inbound_rule" {
    for_each = var.agent_plane_syncthing_sync_public ? toset(["tcp", "udp"]) : toset([])
    content {
      protocol         = inbound_rule.value
      port_range       = "22000"
      source_addresses = ["0.0.0.0/0", "::/0"]
    }
  }

  # Outbound stays WIDE OPEN, and this is not laziness -- it is the whole point.
  #
  # A DO cloud firewall with NO outbound rules blocks ALL egress. The agent plane
  # is a machine whose entire function is outbound: the Anthropic API, GitHub,
  # the DO API, ghcr.io, the Cloudflare tunnel that serves the Paperclip
  # dashboard, OTLP to grove-obs:5080, apt. An inbound-only firewall here would
  # not "harden" the box, it would take the whole company's automation offline in
  # one apply. Never trim these three rules to "tighten" this firewall without
  # first enumerating every egress the agents depend on.
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

###############################################################################
# Variables for this file live HERE, not in variables.tf, deliberately: this is
# a self-contained gated snowflake fence (tls.tf sets the same precedent), and
# the sibling GOL-2566 change is appending its own block to the tail of
# variables.tf -- two un-merged branches appending to the same hunk is a
# conflict for no benefit.
###############################################################################

variable "agent_plane_firewall_enabled" {
  description = "Attach `grove-agent-plane-fw` to the un-codified `agenticos-droplet` snowflake (572389418), fencing its internet-wide :22 and :8384 to var.admin_ip_cidrs. Defaults FALSE so merging this file is not an apply: the box is not Terraform-managed, and this is the host every agent runs on -- fencing it is Josh's deliberate act with his own SSH verified, not a side effect of someone else's plan. Set true in tfvars (or `-var`) when applying; flip back to false to detach."
  type        = bool
  default     = false
}

variable "agent_plane_droplet_name" {
  description = "Name the agent-plane droplet is resolved by, rather than pinning the bare id 572389418 in code. The DO `droplet` data source has no id lookup, and the name is the stable handle a rebuild reuses. Only read when agent_plane_firewall_enabled is true."
  type        = string
  default     = "agenticos-droplet"
}

variable "agent_plane_syncthing_sync_public" {
  description = "Open Syncthing's sync transport (tcp+udp 22000) to the world on the agent plane. FALSE by default because it is not listening: verified 2026-09-29 that tcp/22000 is closed from both the docker bridge gateway and the public IP, so peers are reached OUTBOUND via relays and inbound is unnecessary. Set true only if Syncthing sync degrades after the fence goes on. The GUI (:8384) fence is unaffected either way."
  type        = bool
  default     = false
}
