###############################################################################
# grove-obs — PLATFORM-PLANE resource alerts (GOL-3588, 10-22 audit).
#
# Until this file, grove-obs had NO DigitalOcean alert policy at all: prod
# droplets are covered by the `env-production` tag (production/observability.tf)
# and the AgenticOS droplet by id (AgenticOS infra/terraform/monitor-alerts.tf),
# but grove-obs carries neither. The obs droplet is the one box whose failure
# the obs stack cannot report on itself — a full disk or OOM here silences
# OpenObserve + Keep, and with them every other alert. So the page for THIS box
# must come from DO's control plane, never from Keep.
#
# Scoped by ENTITY id, not a tag: grove-obs is a singleton and its tags
# (local.tags) are shared with nothing we want alerting on here.
#
# The id comes from a READ (data.digitalocean_droplets, name == "grove-obs"),
# NOT from module.obs_droplet.droplet_id (GOL-3816). Referencing the module
# output makes every `-target=digitalocean_monitor_alert.obs_droplet` plan pull
# the droplet into the graph, and this env's droplet is normally pending a
# documented `must be replaced` (user_data is ForceNew and main carries
# codified-for-next-rebuild changes -- see README "Changing cloud-init REPLACES
# the droplet"). A targeted alert apply would then destroy grove-obs. With the
# data source the alerts depend only on the lookup, so an alert-only plan can
# never touch the droplet. On a FRESH build the lookup returns nothing and the
# for_each below is empty: apply once to create the droplet, apply again to get
# the three policies (idempotent, converges on the second run).
#
# The three live policies were created out-of-band with doctl on 2026-11-07 and
# imported into this state (GOL-3816); `terraform import
# 'digitalocean_monitor_alert.obs_droplet["cpu"]' <uuid>` is the recipe if that
# ever has to be repeated.
#
# Delivery mirrors production/observability.tf: email + Discord via the
# Slack-compat shim (`<webhook>/slack` — DO has no Discord target). Reuses
# var.discord_webhook_critical, which this env already injects for Keep, so the
# apply needs no new secret. Note this webhook is called by DO directly, so the
# page still lands when Keep is the thing that is down.
###############################################################################

data "digitalocean_droplets" "obs" {
  filter {
    key      = "name"
    values   = ["grove-obs"]
    match_by = "exact"
  }
}

locals {
  # Exactly one grove-obs droplet is expected; zero only on a fresh build before
  # module.obs_droplet has been applied. More than one is a bug (a half-finished
  # replace) and must fail the plan rather than alert on an arbitrary box.
  obs_alert_droplet_ids = [for d in data.digitalocean_droplets.obs.droplets : tostring(d.id)]

  obs_alert_targets = {
    email   = var.alert_emails
    channel = var.alert_discord_channel
    url     = "${var.discord_webhook_critical}/slack"
  }

  # Thresholds match the prod platform plane for a 2-core box. Disk is the one
  # most likely to bite here: OpenObserve's WAL + local Parquet cache and the
  # Keep DB live on the root disk (volume_size_gb = 0).
  obs_droplet_alerts = {
    cpu = {
      type        = "v1/insights/droplet/cpu"
      value       = 80
      window      = "10m"
      description = "[grove-obs] Droplet CPU > 80% for 10m"
    }

    memory = {
      type        = "v1/insights/droplet/memory_utilization_percent"
      value       = 85
      window      = "5m"
      description = "[grove-obs] Droplet memory > 85% for 5m"
    }

    disk = {
      type        = "v1/insights/droplet/disk_utilization_percent"
      value       = 85
      window      = "5m"
      description = "[grove-obs] Droplet disk > 85% for 5m"
    }
  }
}

resource "digitalocean_monitor_alert" "obs_droplet" {
  # Empty on a fresh build (no grove-obs droplet yet) -> no policies until the
  # second apply. See the header.
  for_each = length(local.obs_alert_droplet_ids) > 0 ? local.obs_droplet_alerts : {}

  lifecycle {
    precondition {
      condition     = length(local.obs_alert_droplet_ids) == 1
      error_message = "Expected exactly one droplet named grove-obs, found ${length(local.obs_alert_droplet_ids)} -- refusing to attach alert policies to an ambiguous entity set."
    }
  }

  type        = each.value.type
  compare     = "GreaterThan"
  value       = each.value.value
  window      = each.value.window
  description = each.value.description
  enabled     = true

  # Requires `monitoring = true` on the droplet (set in main.tf) — without the
  # DO metrics agent these alerts sit permanently green.
  entities = local.obs_alert_droplet_ids

  alerts {
    email = local.obs_alert_targets.email

    slack {
      channel = local.obs_alert_targets.channel
      url     = local.obs_alert_targets.url
    }
  }
}
