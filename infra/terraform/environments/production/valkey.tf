###############################################################################
# Storefront shared data cache - Managed Valkey (GOL-3907, Train #5).
#
# One cache shared by the four storefront apps (hub, goldberry, ggg, nursery).
# grove-sites' @grove/next-cache is a Next.js cacheHandler that keeps the
# storefront's existing local cache when VALKEY_URL is unset and uses this
# cluster when it is set. A shared cache means a publish webhook flushes every
# nursery instance, not just the one it lands on, and a redeploy no longer
# starts each app with an empty cache.
#
# The apps opt in one at a time through var.storefront_cache_apps (apps.tf).
# With the default empty set no app spec changes; the cluster and its
# firewall are pure additions.
#
# No prevent_destroy: this is a rebuildable cache. Losing it only makes pages
# slower until the cache warms again; no data lives only here. To roll back an
# app, remove it from var.storefront_cache_apps (targeted apply).
#
# Apply ONLY with -target, never concurrently with another prod apply:
#   terraform apply -target=digitalocean_database_cluster.valkey \
#                   -target=digitalocean_database_firewall.valkey \
#                   -target=digitalocean_database_valkey_config.valkey
# The firewall references the four apps, so -target pulls them into the plan;
# with storefront_cache_apps = [] they must show no change.
###############################################################################

resource "digitalocean_database_cluster" "valkey" {
  name       = "grove-prod-valkey"
  engine     = "valkey"
  version    = "8"
  size       = "db-s-1vcpu-1gb" # ~$15/mo
  region     = var.region
  node_count = 1
  tags       = concat(local.tags, ["role-cache"])
}

# Eviction: volatile-lru evicts only keys that carry a TTL (cache entries), so a
# full cache drops old pages instead of erroring, and keys written without a
# TTL (e.g. tag-invalidation records) are never evicted -- evicting one of
# those could resurrect a stale page. DO's default policy is noeviction.
resource "digitalocean_database_valkey_config" "valkey" {
  cluster_id              = digitalocean_database_cluster.valkey.id
  valkey_maxmemory_policy = "volatile-lru"
}

# Trusted sources: only the four storefront apps. type = "app" takes the App
# Platform app id, so the rule follows the app across redeploys. No operator
# CIDR: nobody needs a shell on a cache, and `doctl databases` covers admin.
resource "digitalocean_database_firewall" "valkey" {
  cluster_id = digitalocean_database_cluster.valkey.id

  rule {
    type  = "app"
    value = digitalocean_app.hub.id
  }

  dynamic "rule" {
    for_each = digitalocean_app.tenant
    content {
      type  = "app"
      value = rule.value.id
    }
  }
}
