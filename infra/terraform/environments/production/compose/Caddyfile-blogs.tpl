# Caddyfile for the production blogs droplet.
# TLS: Cloudflare Origin CA certs (files under /certs, mounted ro).
# All hostnames are Cloudflare-proxied; the CF edge holds the public cert.
#
# blog.* headless-demote (GOL-1530): when blog_headless_demote_enabled is true
# (flipped ONLY in the GOL-1279 apex-cutover window — see
# docs/RUNBOOK-apex-launch-cutover.md Step 2b), each blog.* vhost stops serving
# the reader-facing Ghost site and instead:
#   - passes through ONLY the headless surface (/ghost + /ghost/* = admin +
#     Admin/Content API, /content/* = images/media, /members/* = Ghost members)
#     so Ghost admin, both APIs, images and members keep working; and
#   - 301-redirects every OTHER path to the brand's React blog route on the apex
#     (hub -> /journal/{slug}; goldberry -> /blog/{slug}; ggg + nursery -> apex
#     root, no per-slug map until real posts exist — GOL-1113/GOL-1284).
#
# Default false => byte-identical passthrough to the pre-cutover config, so the
# committed PR is a no-op. It MUST NOT flip before the apex DNS repoint or readers
# loop (apex 302 -> blog 301 -> apex). The apex side is the narrow /content/* 301
# in redirects.tf (GOL-1282/#426); this is the blog.* side (GOL-1528/GOL-1530).

gatheringatthegrove.com {
	tls /certs/gatheringatthegrove.com.pem /certs/gatheringatthegrove.com.key
	reverse_proxy ghost-hub:2368 {
		header_up X-Real-IP {remote_host}
		header_up X-Forwarded-For {remote_host}
		header_up X-Forwarded-Proto {scheme}
		header_up X-Forwarded-Host {host}
	}
}

blog.gatheringatthegrove.com {
	tls /certs/gatheringatthegrove.com.pem /certs/gatheringatthegrove.com.key
	header X-Robots-Tag "noindex, nofollow"
%{ if blog_headless_demote_enabled ~}
	# Headless: only the API/admin/media/members surface is proxied to Ghost.
	@headless path /ghost /ghost/* /content/* /members/*
	handle @headless {
		reverse_proxy ghost-hub:2368 {
			header_up X-Real-IP {remote_host}
			header_up X-Forwarded-For {remote_host}
			header_up X-Forwarded-Proto {scheme}
			header_up X-Forwarded-Host {host}
		}
	}
	# Every reader path 301s to the hub's React journal route on the apex.
	handle {
		redir https://gatheringatthegrove.com/journal{uri} 301
	}
%{ else ~}
	reverse_proxy ghost-hub:2368 {
		header_up X-Real-IP {remote_host}
		header_up X-Forwarded-For {remote_host}
		header_up X-Forwarded-Proto {scheme}
		header_up X-Forwarded-Host {host}
	}
%{ endif ~}
}

goldberrygrove.farm {
	tls /certs/goldberrygrove.farm.pem /certs/goldberrygrove.farm.key
	reverse_proxy ghost-goldberry:2368 {
		header_up X-Real-IP {remote_host}
		header_up X-Forwarded-For {remote_host}
		header_up X-Forwarded-Proto {scheme}
		header_up X-Forwarded-Host {host}
	}
}

blog.goldberrygrove.farm {
	tls /certs/goldberrygrove.farm.pem /certs/goldberrygrove.farm.key
	header X-Robots-Tag "noindex, nofollow"
%{ if blog_headless_demote_enabled ~}
	# Headless: only the API/admin/media/members surface is proxied to Ghost.
	@headless path /ghost /ghost/* /content/* /members/*
	handle @headless {
		reverse_proxy ghost-goldberry:2368 {
			header_up X-Real-IP {remote_host}
			header_up X-Forwarded-For {remote_host}
			header_up X-Forwarded-Proto {scheme}
			header_up X-Forwarded-Host {host}
		}
	}
	# Every reader path 301s to the storefront's React blog route on the apex.
	handle {
		redir https://goldberrygrove.farm/blog{uri} 301
	}
%{ else ~}
	reverse_proxy ghost-goldberry:2368 {
		header_up X-Real-IP {remote_host}
		header_up X-Forwarded-For {remote_host}
		header_up X-Forwarded-Proto {scheme}
		header_up X-Forwarded-Host {host}
	}
%{ endif ~}
}

blog.woodworkingeorge.com {
	tls /certs/woodworkingeorge.com.pem /certs/woodworkingeorge.com.key
	header X-Robots-Tag "noindex, nofollow"
%{ if blog_headless_demote_enabled ~}
	# Headless: only the API/admin/media/members surface is proxied to Ghost.
	@headless path /ghost /ghost/* /content/* /members/*
	handle @headless {
		reverse_proxy ghost-ggg:2368 {
			header_up X-Real-IP {remote_host}
			header_up X-Forwarded-For {remote_host}
			header_up X-Forwarded-Proto {scheme}
			header_up X-Forwarded-Host {host}
		}
	}
	# No per-slug map yet (no real posts) — every reader path 301s to apex root.
	handle {
		redir https://woodworkingeorge.com/ 301
	}
%{ else ~}
	reverse_proxy ghost-ggg:2368 {
		header_up X-Real-IP {remote_host}
		header_up X-Forwarded-For {remote_host}
		header_up X-Forwarded-Proto {scheme}
		header_up X-Forwarded-Host {host}
	}
%{ endif ~}
}

blog.atthegrovenursery.com {
	tls /certs/atthegrovenursery.com.pem /certs/atthegrovenursery.com.key
	header X-Robots-Tag "noindex, nofollow"
%{ if blog_headless_demote_enabled ~}
	# Headless: only the API/admin/media/members surface is proxied to Ghost.
	@headless path /ghost /ghost/* /content/* /members/*
	handle @headless {
		reverse_proxy ghost-nursery:2368 {
			header_up X-Real-IP {remote_host}
			header_up X-Forwarded-For {remote_host}
			header_up X-Forwarded-Proto {scheme}
			header_up X-Forwarded-Host {host}
		}
	}
	# No per-slug map yet (nursery posts land via GOL-1113/#442) — 301 to apex root.
	handle {
		redir https://atthegrovenursery.com/ 301
	}
%{ else ~}
	reverse_proxy ghost-nursery:2368 {
		header_up X-Real-IP {remote_host}
		header_up X-Forwarded-For {remote_host}
		header_up X-Forwarded-Proto {scheme}
		header_up X-Forwarded-Host {host}
	}
%{ endif ~}
}
