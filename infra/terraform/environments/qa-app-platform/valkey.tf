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
###############################################################################

resource "digitalocean_database_cluster" "valkey" {
  name       = "grove-qa-l3-valkey"
  engine     = "valkey"
  version    = "8"
  size       = "db-s-1vcpu-1gb"
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

# Trusted sources: only the four QA storefront apps.
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
