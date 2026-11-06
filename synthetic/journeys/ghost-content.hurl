# Synthetic Tier-1 journey: Ghost Content API answers (per tenant).
# Hits the Ghost Content API v5 with the tenant's content key — a content-API
# check beyond the ghost-*-admin availability monitors. ghost_url + ghost_key
# are injected per tenant by run.py (key via a variables-file so it stays out of
# argv).
# Catches: Ghost content key rotated/invalid (Ghost 401s a key from another
# site), Content API broken — which would make the tenant's /blog page fail even
# though Ghost is "up".
# An EMPTY blog is NOT a failure: the storefront /blog renders an empty state
# (grove-sites #897), and prod nursery Ghost has 0 published posts, which paged
# as a false outage at go-live (GOL-2329). Missing content is a content task.
GET {{ghost_url}}/ghost/api/content/posts/?key={{ghost_key}}&limit=1
HTTP 200
[Asserts]
jsonpath "$.posts" isCollection
jsonpath "$.meta.pagination" exists
