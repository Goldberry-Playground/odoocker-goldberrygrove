# Caddyfile for the production Odoo droplet (ADR-007 Phase 6, GOL-105).
# Fronts ONLY Odoo - the 4 frontends are on App Platform, blogs on their own
# droplet. TLS: Cloudflare Origin CA cert files (under /certs, mounted ro);
# the hostname is Cloudflare-proxied, so the CF edge holds the public cert and
# talks to this origin over the Origin CA cert. No ACME, no DNS plugin - same
# pattern as the blogs Caddyfile.
#
# The cert is the hub-zone Origin CA cert (blogs.tf); its
# `*.gatheringatthegrove.com` SAN covers odoo.gatheringatthegrove.com.

# GOL-3587: no admin API. Nothing on this box drives Caddy over :2019 (config
# is baked at boot; a change = droplet rebuild), so the endpoint is pure attack
# surface for any process sharing the container network. Mirrors
# Caddyfile-rum.tpl. NOTE: `caddy reload` needs the admin API — restart the
# container instead.
{
	admin off
}

odoo.gatheringatthegrove.com {
	tls /certs/gatheringatthegrove.com.pem /certs/gatheringatthegrove.com.key

	# GOL-3587: the Odoo database manager (create/drop/backup/restore) is never
	# reachable through the public front door. Prod already runs LIST_DB=false,
	# but that only hides the DB list — /web/database/* still answers and is
	# guarded solely by admin_passwd. Defence in depth: 404 at the edge.
	@dbmgr path /web/database /web/database/*
	respond @dbmgr 404

	# Longpolling + websocket endpoints - must NOT be buffered/compressed; pass
	# through raw. Odoo's chat + bus notifications use /longpolling (pre-19) and
	# /websocket (the Odoo 19 bus). Both bind on the evented worker (8072), not
	# the HTTP worker (8069), or they 500 with "Couldn't bind the websocket".
	@longpoll {
		path /longpolling/* /websocket*
	}
	reverse_proxy @longpoll odoo:8072

	# Everything else - main Odoo HTTP backend. PROXY_MODE=true makes Odoo
	# trust these X-Forwarded-* headers for CSRF + URL generation.
	reverse_proxy odoo:8069 {
		header_up X-Real-IP {remote_host}
		header_up X-Forwarded-For {remote_host}
		header_up X-Forwarded-Proto {scheme}
		header_up X-Forwarded-Host {host}
	}
}
