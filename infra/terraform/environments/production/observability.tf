###############################################################################
# Production observability — PLATFORM-PLANE alerting (GOL-381, from the
# GOL-379 cutover audit).
#
# DELIBERATELY INDEPENDENT OF `grove-obs`. Everything in this file is evaluated
# and delivered by DigitalOcean's own control plane, not by the OpenObserve +
# Keep stack on the observability droplet (environments/observability). That is
# the entire point:
#
#   obs plane (grove-obs)   : rich, queryable, correlated — and a droplet that
#                             can itself be down, wedged, or full.
#   platform plane (here)   : coarse (CPU/RAM/disk/uptime), but it survives the
#                             obs droplet being dead. If grove-obs is the thing
#                             that fell over, THIS is what still pages us.
#
# Two independent planes is not redundancy for its own sake — a monitoring
# system that shares a failure domain with the thing it monitors reports "all
# green" precisely when it matters most.
#
# ── SCOPING: TAGS, NOT ENTITY IDs ────────────────────────────────────────────
# Alerts bind to the `env-production` TAG rather than droplet IDs. Consequences,
# all of them wanted:
#   * grove-prod-odoo does not exist yet (Track 2 apply is GOL-105-gated). A
#     tag-scoped alert has nothing to reference, so this file applies TODAY
#     against the live blogs droplet without waiting on the Odoo cutover.
#   * When grove-prod-odoo DOES land, it is born tagged `env-production` and is
#     covered the instant it boots — no second apply, no "we forgot to add the
#     new box to monitoring" gap. That gap is the classic way a launch-day
#     droplet ends up unmonitored.
#   * A droplet is never silently dropped from alerting by an ID drifting.
#
# ── PREREQUISITE: THE DO METRICS AGENT ───────────────────────────────────────
# `v1/insights/droplet/*` alerts are computed from the DO metrics agent, NOT the
# hypervisor. Without `monitoring = true` on the droplet the agent is absent,
# the metric stream is empty, and these alerts sit permanently green — the worst
# possible failure mode, because it looks like success. blogs.tf/odoo.tf
# therefore set `monitoring = true`; that flag and this file are one change and
# must not be split.
#
# ── WHY DISCORD ROUTING GOES THROUGH THE `slack` BLOCK ───────────────────────
# DO's alert API delivers to email or Slack only — there is no Discord target.
# Discord implements a Slack-compatible webhook shim at `<webhook-url>/slack`,
# so DO's Slack payload is accepted verbatim. Verified live 2026-07-15 (GOL-381)
# by POSTing a DO-shaped Slack alert payload to the real webhook + `/slack`:
# HTTP 200, message rendered in #grove-ops. The `channel` field below is inert
# (Discord derives the channel from the webhook itself) but the provider marks
# it required, so it carries the human-readable destination name.
#
# Email is kept ALONGSIDE Discord on every alert on purpose: it is the path that
# does not depend on Discord being reachable, on the webhook not being rotated,
# and on anyone having Discord open at 2am.
###############################################################################

locals {
  # Every production droplet carries this tag (local.tags in main.tf), so this
  # is the join key between "is production compute" and "is alerted on".
  prod_droplet_tag = "env-production"

  # Discord's Slack-compat shim. Suffixing the webhook is what makes DO's
  # Slack-shaped payload land in Discord — see the header note.
  discord_slack_webhook = "${var.discord_webhook_url}/slack"

  # Fan-out applied identically to every alert below. Defined once so a routing
  # change (new webhook, added responder) cannot be applied to some alerts and
  # forgotten on others — partial routing is how an alert goes unnoticed.
  alert_targets = {
    email   = var.alert_emails
    channel = var.alert_discord_channel
    url     = "${var.discord_webhook_url}/slack"
  }

  # Droplet resource alerts. Thresholds are deliberately conservative for a
  # 2-core box: the goal is a page that a human ACTS on, not a feed they learn
  # to ignore. An alert that fires weekly and is always benign is worse than no
  # alert, because it trains the responder to swipe it away.
  #
  # window: DO evaluates the metric averaged over this period. Short windows on
  # CPU produce noise from cron/backup spikes; disk moves slowly so a short
  # window is fine and buys earlier warning.
  droplet_alerts = {
    cpu = {
      type        = "v1/insights/droplet/cpu"
      value       = 80
      window      = "10m"
      description = "[prod] Droplet CPU > 80% for 10m"
    }

    memory = {
      type        = "v1/insights/droplet/memory_utilization_percent"
      value       = 85
      window      = "5m"
      description = "[prod] Droplet memory > 85% for 5m"
    }

    # 85%, not 90%: the blogs droplet writes MySQL + Ghost content + nightly
    # backup tarballs to the same volume. The gap between 85% and full is the
    # operator's response time — at 90% on a small disk that is minutes.
    disk = {
      type        = "v1/insights/droplet/disk_utilization_percent"
      value       = 85
      window      = "5m"
      description = "[prod] Droplet disk > 85% for 5m"
    }

    # Load is the signal that survives a CPU-accounting blind spot: heavy iowait
    # (a stalled volume, a thrashing MySQL) shows up as load long before it
    # shows up as CPU%. Threshold is per-box-cores-dependent; 4 = 2x the core
    # count on the current s-2vcpu-* prod droplets.
    load5 = {
      type        = "v1/insights/droplet/load_5"
      value       = 4
      window      = "10m"
      description = "[prod] Droplet 5m load average > 4 (2x cores) for 10m"
    }
  }
}

# ── Droplet resource alerts (platform plane) ─────────────────────────────────

resource "digitalocean_monitor_alert" "droplet" {
  for_each = local.droplet_alerts

  type        = each.value.type
  compare     = "GreaterThan"
  value       = each.value.value
  window      = each.value.window
  description = each.value.description
  enabled     = true

  # Tag-scoped, NOT entity-scoped — see header. `entities` is intentionally
  # omitted; setting both is an API error.
  tags = [local.prod_droplet_tag]

  alerts {
    email = local.alert_targets.email

    slack {
      channel = local.alert_targets.channel
      url     = local.alert_targets.url
    }
  }
}

# ── Public-surface uptime checks (platform plane) ────────────────────────────
# Run from DO's global probe network — outside our infrastructure entirely. This
# is the only signal here that does not depend on ANY Grove host being alive,
# including the droplet being probed. It answers the question the resource
# alerts cannot: "can a customer actually load the site right now?"
#
# HONEST LIMITATION — these probe the Cloudflare edge, not the origin, because
# the origin presents a Cloudflare Origin CA cert that is not publicly trusted
# (a direct-to-IP HTTPS probe fails cert validation by design). CF can therefore
# serve a cached page while the origin is dead, and this check stays green. That
# is a real blind spot and it is why the resource alerts above and the obs-plane
# work (GOL-381 remaining items) are not optional. Measured 2026-07-15: a
# `cf-cache-status: HIT` masked a 404 on two blog hosts.
#
# Targets are variable-driven and default to the hosts VERIFIED healthy at
# authoring time. blog.gatheringatthegrove.com + blog.goldberrygrove.farm are
# excluded because they were serving 404 on 2026-07-15 (tracked separately) —
# wiring a check to a known-broken target ships a permanently-red alert, and a
# permanently-red alert is indistinguishable from no alert within a week.

resource "digitalocean_uptime_check" "public" {
  for_each = var.uptime_check_targets

  name    = "prod-${each.key}"
  target  = each.value
  type    = "https"
  enabled = true

  # Multi-region so a single probe-region network blip is not a page. Pairs with
  # `down_global` below, which requires agreement across regions.
  regions = ["us_east", "us_west", "eu_west"]
}

resource "digitalocean_uptime_alert" "down" {
  for_each = digitalocean_uptime_check.public

  check_id = each.value.id
  name     = "prod-${each.key}-down"

  # down_global (not `down`): fires only when the target is unreachable from
  # EVERY probe region. Single-region `down` pages on the internet being the
  # internet; this pages on the site actually being gone.
  type       = "down_global"
  comparison = "greater_than"
  period     = "2m"
  threshold  = 1

  notifications {
    email = local.alert_targets.email

    slack {
      channel = local.alert_targets.channel
      url     = local.alert_targets.url
    }
  }
}

# TLS expiry. The brand apexes are Cloudflare-proxied, so this watches the EDGE
# cert (CF-managed, auto-renewed) — low risk, near-zero cost to watch.
#
# It does NOT cover the Cloudflare Origin CA certs that Caddy serves (blogs.tf /
# odoo.tf): those are 15-year certs and are invisible to a public probe. They
# are a 2041 problem, but an undated one — no alert anywhere in this estate
# fires before they lapse. Noted so the next reader does not mistake this alert
# for full TLS coverage.
resource "digitalocean_uptime_alert" "ssl_expiry" {
  for_each = digitalocean_uptime_check.public

  check_id = each.value.id
  name     = "prod-${each.key}-ssl-expiry"

  type       = "ssl_expiry"
  comparison = "less_than"
  period     = "1h"
  threshold  = 14 # days of remaining validity

  notifications {
    email = local.alert_targets.email

    slack {
      channel = local.alert_targets.channel
      url     = local.alert_targets.url
    }
  }
}
