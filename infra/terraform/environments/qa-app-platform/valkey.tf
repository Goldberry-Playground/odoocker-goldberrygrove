###############################################################################
# Storefront shared data cache - QA Managed Valkey (GOL-3907, Train #5).
#
# QA twin of environments/production/valkey.tf. Unlike prod, every QA
# storefront app always gets VALKEY_URL + GROVE_CACHE_NAMESPACE (apps.tf), so
# the Train #5 grove-sites checks (cross-instance flush, warm after redeploy,
# cache-outage fallback) run against a real cache. QA has its own cluster and
# never points at grove-prod-valkey.
#
# Lives only for the train window: `scripts/qa-l3-teardown.sh compute`
# destroys it with the apps, and `make qa-l3-up` re-creates it. No
# prevent_destroy (a cache holds nothing that is not rebuildable).
#
# VALKEY_URL is RUN_TIME scope ONLY -- never BUILD_TIME / RUN_AND_BUILD_TIME.
# `next build` must run with the cache off, and the grove-sites Dockerfiles
# deliberately have no `ARG VALKEY_URL`; keep both that way.
###############################################################################

resource "digitalocean_database_cluster" "valkey" {
  name       = "grove-qa-l3-valkey"
  engine     = "valkey"
  version    = "8"
  size       = "db-s-1vcpu-1gb"
  region     = var.region
  node_count = 1
  tags       = concat(local.tags, ["role-cache"])

  # GROVE_CACHE_NAMESPACE must be unique per app (apps.tf sets "hub" on the hub
  # and the tenant key on each tenant). Two apps on one namespace would not
  # leak data but would share the warm lock and the grove:<ns>:tags hash, so
  # one app's flush/warm would stomp the other's. Fails the plan if a tenant
  # key ever collides with "hub".
  lifecycle {
    precondition {
      condition     = length(distinct(local.storefront_cache_namespaces)) == length(local.storefront_cache_namespaces)
      error_message = "GROVE_CACHE_NAMESPACE values must be unique per storefront app; a tenant key collides with \"hub\"."
    }
  }
}

locals {
  # One GROVE_CACHE_NAMESPACE per storefront app, as set in apps.tf.
  storefront_cache_namespaces = concat(["hub"], keys(local.tenant_apps))

  # VALKEY_URL the QA storefronts get (apps.tf). var.valkey_outage_mode swaps
  # in a dead address for the Train #5 cache-outage test; pages must still
  # serve from the local fallback in both forms:
  #   hang    -> 192.0.2.1 (TEST-NET-1, RFC 5737): packets are dropped, so
  #              connects hang until the client timeout.
  #   refused -> 127.0.0.1:6379 inside the container: nothing listens, so the
  #              connect is refused immediately.
  # Each switch is one targeted apply and a redeploy of the four QA apps.
  qa_valkey_url = {
    ""      = digitalocean_database_cluster.valkey.uri
    hang    = "rediss://default:outage-test@192.0.2.1:25061"
    refused = "redis://default:outage-test@127.0.0.1:6379"
  }[var.valkey_outage_mode]
}

# Eviction: volatile-lru evicts only keys that carry a TTL (cache entries), so a
# full cache drops old pages instead of erroring, and keys written without a
# TTL (e.g. tag-invalidation records) are never evicted -- evicting one of
# those could resurrect a stale page. DO's default policy is noeviction.
resource "digitalocean_database_valkey_config" "valkey" {
  cluster_id              = digitalocean_database_cluster.valkey.id
  valkey_maxmemory_policy = "volatile-lru"
}

# Trusted sources: only the four QA storefront apps.
resource "digitalocean_database_firewall" "valkey" {
  cluster_id = digitalocean_database_cluster.valkey.id

  rule {
    type  = "app"
    value = digitalocean_app.hub.id
  }

  # Operator IPs for the Train #5 QA gate check that grove:<ns>:d:* keys exist
  # (`valkey-cli --tls -u <uri> --scan --pattern 'grove:*:d:*'`). Default
  # empty; QA only -- prod admits apps alone.
  dynamic "rule" {
    for_each = var.valkey_operator_cidrs
    content {
      type  = "ip_addr"
      value = rule.value
    }
  }

  dynamic "rule" {
    for_each = digitalocean_app.tenant
    content {
      type  = "app"
      value = rule.value.id
    }
  }
}
