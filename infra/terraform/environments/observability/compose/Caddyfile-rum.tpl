# grove-obs PUBLIC RUM ingest vhost (GOL-311).
#
# Browser Real-User-Monitoring beacons from the six tenant storefronts POST to:
#   https://${rum_public_host}/rum/v1/default/rum?o2source=browser&o2-api-key=<client-token>
# (contract reverse-engineered from @openobserve/browser-rum). Unlike the admin
# OpenObserve UI (5080, admin-CIDR only) and the cross-plane OTLP ingest, RUM
# ingests from real user BROWSERS, so this vhost is the ONLY public surface on
# grove-obs. Three guardrails keep that surface tight:
#   1. PATH-RESTRICTED to /rum/* -> the OpenObserve admin UI/API/login are never
#      reachable through this hostname (everything else 404s here).
#   2. CORS-scoped: Access-Control-Allow-Origin is reflected ONLY for the tenant
#      + preview origins in ${cors_origin_regex}; any other Origin gets no ACAO
#      header and the browser blocks the cross-origin read.
#   3. Origin-locked at the firewall: :443 is open ONLY to Cloudflare edge IPs
#      (see main.tf), so the droplet port is not exposed to the whole internet.
#
# TLS: a Cloudflare Origin Certificate (mounted from cloud-init at
# /etc/caddy/certs) so Cloudflare's proxy (orange-cloud) connects Full (Strict).
# Public browser TLS terminates at the Cloudflare edge; the edge->origin hop is
# authenticated by this cert. No ACME here on purpose: the grove-caddy DO DNS-01
# plugin cannot validate a Cloudflare-hosted zone, and a mounted 15-year origin
# cert needs no renewal. Because we never issue certs, use the plain upstream
# caddy image (no plugin) and disable auto_https.
{
	auto_https off
	admin off
}

${rum_public_host}:443 {
	tls /etc/caddy/certs/rum.crt /etc/caddy/certs/rum.key

	# Reflect Origin ONLY for the tenant + preview origins. RE2 (Go) regexp.
	@rum_cors header_regexp origin Origin ${cors_origin_regex}

	# Public RUM ingest paths only. NOTE: `handle` (not `handle_path`) so the
	# full /rum/v1/default/rum path reaches OpenObserve unstripped.
	@rum_path path /rum/*
	handle @rum_path {
		# Our CORS is authoritative; `defer` applies it after the proxy response
		# and the reverse_proxy strips any upstream ACA-* so headers never dup.
		header @rum_cors {
			Access-Control-Allow-Origin "{http.request.header.Origin}"
			Access-Control-Allow-Methods "POST, OPTIONS"
			Access-Control-Allow-Headers "Content-Type"
			Access-Control-Max-Age "86400"
			Vary "Origin"
			defer
		}

		# Preflight is answered here; never forward OPTIONS upstream.
		@preflight method OPTIONS
		respond @preflight 204

		reverse_proxy openobserve:5080 {
			# X-Forwarded-For/Proto/Host are passed by Caddy's default; only
			# X-Real-IP needs to be set explicitly. Strip any upstream CORS so
			# the deferred header block above is the single source of truth.
			header_up X-Real-IP {remote_host}
			header_down -Access-Control-Allow-Origin
			header_down -Access-Control-Allow-Methods
			header_down -Access-Control-Allow-Headers
			header_down -Access-Control-Max-Age
		}
	}

	# Everything else is closed so the OpenObserve UI/API stay private.
	handle {
		respond "grove-obs: not found" 404
	}
}
%{ if otlp_ingest_enabled ~}

# grove-obs PUBLIC OTLP ingest vhost (GOL-2330) -- AUTHORED-BUT-INERT until
# var.otlp_ingest_* are all set (the %%{ if } above drops this whole block, so
# the rendered Caddyfile -- and therefore user_data -- is byte-identical while
# disabled and no droplet replace is planned).
#
# Off-droplet OTLP shippers (tier-2 GitHub-Actions Playwright journeys) POST to:
#   https://${otlp_ingest_host}/api/default/v1/{traces,metrics,logs}
#   with `Authorization: Bearer <otlp-ingest-token>`.
# Three layers:
#   1. Cloudflare WAF (cloudflare-policy env) 403s any request to this host that
#      lacks the exact Bearer, BEFORE origin-pull.
#   2. This vhost RE-CHECKS the same Bearer. :443 is CF-IP-locked, but CF IPs
#      are shared by every CF customer -- anyone could point their own zone at
#      this origin and skip our WAF. The origin check closes that bypass.
#   3. The client Bearer is REPLACED with OpenObserve's own Basic credential
#      (header_up), so the shipper never holds an OpenObserve credential and the
#      ingest token rotates independently of OpenObserve.
# Path+method-restricted to OTLP/HTTP ingest POSTs; everything else 403s.
${otlp_ingest_host}:443 {
	tls /etc/caddy/certs/otlp.crt /etc/caddy/certs/otlp.key

	@otlp_authorized {
		method POST
		path_regexp otlp ^/api/[A-Za-z0-9_-]+/v1/(traces|metrics|logs)$
		header Authorization "Bearer ${otlp_ingest_bearer_token}"
	}
	handle @otlp_authorized {
		reverse_proxy openobserve:5080 {
			header_up Authorization "Basic ${otlp_upstream_basic_b64}"
			header_up X-Real-IP {remote_host}
		}
	}

	handle {
		respond "grove-obs: forbidden" 403
	}
}
%{ endif ~}
