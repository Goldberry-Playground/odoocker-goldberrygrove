# Caddyfile for the production Odoo droplet (ADR-007 Phase 6, GOL-105).
# Fronts ONLY Odoo - the 4 frontends are on App Platform, blogs on their own
# droplet. TLS: Cloudflare Origin CA cert files (under /certs, mounted ro);
# the hostname is Cloudflare-proxied, so the CF edge holds the public cert and
# talks to this origin over the Origin CA cert. No ACME, no DNS plugin - same
# pattern as the blogs Caddyfile.
#
# The cert is the hub-zone Origin CA cert (blogs.tf); its
# `*.gatheringatthegrove.com` SAN covers odoo.gatheringatthegrove.com.

odoo.gatheringatthegrove.com {
	tls /certs/gatheringatthegrove.com.pem /certs/gatheringatthegrove.com.key

	# Longpolling endpoint - must NOT be buffered/compressed; pass through raw.
	# Odoo's chat + workflow notifications use this.
	@longpoll {
		path /longpolling/*
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
