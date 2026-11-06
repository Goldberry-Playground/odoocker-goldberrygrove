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
# Scoped by ENTITY id (the module output), not a tag: grove-obs is a singleton
# and its tags (local.tags) are shared with nothing we want alerting on here.
#
# Delivery mirrors production/observability.tf: email + Discord via the
# Slack-compat shim (`<webhook>/slack` — DO has no Discord target). Reuses
# var.discord_webhook_critical, which this env already injects for Keep, so the
# apply needs no new secret. Note this webhook is called by DO directly, so the
# page still lands when Keep is the thing that is down.
###############################################################################

locals {
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
  for_each = local.obs_droplet_alerts

  type        = each.value.type
  compare     = "GreaterThan"
  value       = each.value.value
  window      = each.value.window
  description = each.value.description
  enabled     = true

  # Requires `monitoring = true` on the droplet (set in main.tf) — without the
  # DO metrics agent these alerts sit permanently green.
  entities = [module.obs_droplet.droplet_id]

  alerts {
    email = local.obs_alert_targets.email

    slack {
      channel = local.obs_alert_targets.channel
      url     = local.obs_alert_targets.url
    }
  }
}
