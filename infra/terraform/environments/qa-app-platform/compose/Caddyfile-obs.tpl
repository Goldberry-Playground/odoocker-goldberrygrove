# Caddyfile for Level 3 obs droplet.
#
# Two admin-only hostnames (firewall allowlists the operator CIDR for 443):
#   oo.${QA_ZONE}    -> OpenObserve UI
#   keep.${QA_ZONE}  -> Keep UI + API
#
# Both use DNS-01 ACME via the DO API (same plugin grove-caddy bakes in).
# Two LE identifiers + 2 renewals/year = trivial rate-limit cost.

{
	acme_ca {env.ACME_CA}
}

oo.${QA_ZONE} {
	tls {
		dns digitalocean {env.DO_API_TOKEN}
	}

	reverse_proxy openobserve:5080 {
		header_up X-Real-IP {remote_host}
		header_up X-Forwarded-For {remote_host}
		header_up X-Forwarded-Proto {scheme}
		header_up X-Forwarded-Host {host}
	}
}

keep.${QA_ZONE} {
	tls {
		dns digitalocean {env.DO_API_TOKEN}
	}

	# Keep API at /api/* -> keep-backend:8080
	# Keep UI at everything else -> keep-frontend:3000
	# Frontend reverse-proxies API calls; this split lets the frontend
	# fetch from same-origin without CORS.
	handle /api/* {
		uri strip_prefix /api
		reverse_proxy keep-backend:8080 {
			header_up X-Real-IP {remote_host}
			header_up X-Forwarded-For {remote_host}
			header_up X-Forwarded-Proto {scheme}
		}
	}

	handle {
		reverse_proxy keep-frontend:3000 {
			header_up X-Real-IP {remote_host}
			header_up X-Forwarded-For {remote_host}
			header_up X-Forwarded-Proto {scheme}
			header_up X-Forwarded-Host {host}
		}
	}
}
