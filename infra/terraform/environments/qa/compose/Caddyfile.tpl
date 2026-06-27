# Grove QA -- Caddyfile template.
#
# TLS via DO DNS-01 wildcard. ONE cert covers the apex + every tenant
# subdomain (qa.gatheringatthegrove.com + *.qa.gatheringatthegrove.com),
# regardless of how many tenants exist. This eliminates LE's per-identifier
# rate limit (5 duplicate certs / 168h) that bit us on 2026-06-25 with
# HTTP-01 + 5 separate identifiers. Mirror of preview env's pattern --
# image: slothcored/caddy:digitalocean ships the DO DNS plugin.
#
# Substituted at TF apply time (NOT cloud-init) via:
#   replace(caddyfile_tpl, "$${QA_ZONE}", qa_zone)
# inside infra/terraform/environments/qa/main.tf. The pattern below uses
# SINGLE-dollar (${QA_ZONE}) because the replace() search string
# "$${QA_ZONE}" becomes the literal "${QA_ZONE}" per TF template escaping.
# Don't "escape" the ${QA_ZONE} below -- leave them single-dollar.
#
# All upstreams are on the default Docker bridge network -- Caddy resolves
# them by service name (hub, goldberry, ggg, nursery, odoo).

{
    email ops@goldberrygrove.farm
    # ACME directory -- defaults to LE PROD but can be flipped to LE STAGING
    # via the ACME_CA env var (set by docker-compose, which gets it from
    # /etc/grove/.env, which gets it from TF var.acme_endpoint). Staging
    # produces certs with browser warnings but has effectively unlimited
    # rate limits -- use when iterating heavily to avoid burning prod budget.
    acme_ca {env.ACME_CA}
}

# Wildcard ONLY -- apex deliberately excluded. The apex is intentionally
# unrouted per ADR-006 (hub canonically serves at hub.qa.* not the bare
# apex). Including the apex in the site block address list ALSO triggers
# a Caddyfile parser quirk where the FIRST host matcher in the block has
# its value replaced with the apex (verified 2026-06-27: @hub matcher
# compiled to `host: [qa.gatheringatthegrove.com]` instead of the
# `hub.qa.gathering` value declared in source). Dropping the apex from
# the address list both removes the unnecessary identifier from the cert
# AND sidesteps the parser bug. Apex requests hit Caddy with no matching
# site block -> Caddy default-404s at the server level.
*.${QA_ZONE} {
    tls {
        # Multi-issuer with explicit fallback. Caddy tries issuers in order;
        # on non-retryable failure (e.g. LE 429 rate limit) it advances to
        # the next. Without this, a prod 429 leaves the deploy permanently
        # stuck on cert provisioning -- exactly what bit us on 2026-06-26.
        #
        # Primary: whatever ACME_CA env says (defaults to LE prod via
        # PR-B's var.acme_endpoint; flips to LE staging when operator
        # passes use_staging_acme=true to qa-deploy).
        # Caddy subdirective for ACME directory URL inside `issuer acme {}` is
        # `dir`, not `ca` (the latter is only valid as a global option:
        # `acme_ca <url>`). PR-D (#98) shipped `ca` and crashed Caddy on the
        # 2026-06-27 cascade — fixed in PR #116 by switching to `dir`.
        issuer acme {
            dir {env.ACME_CA}
            dns digitalocean {env.DO_API_TOKEN}
        }
        # Fallback: LE staging. Browser warnings but no rate limits. Lets a
        # deploy complete cleanly even when prod is rate-limited (the
        # alternative being a stuck droplet with no cert at all).
        issuer acme {
            dir https://acme-staging-v02.api.letsencrypt.org/directory
            dns digitalocean {env.DO_API_TOKEN}
        }
    }

    log {
        output stdout
        format console
    }

    # Route by exact Host header to the right upstream container.
    # Tenant frontends in grove-sites listen on 3001; hub on 3000.
    #
    # IMPORTANT: Caddy's strict parser rejects single-line `handle X { ... }`
    # blocks with "Unexpected next token after '{' on same line" -- the brace
    # must be at end-of-line, contents on subsequent indented lines. This
    # crashlooped the caddy container on first DNS-01 deploy until fixed
    # inline; see git log on 2026-06-26 for the incident.
    @hub       host hub.${QA_ZONE}
    @goldberry host goldberry.${QA_ZONE}
    @ggg       host ggg.${QA_ZONE}
    @nursery   host nursery.${QA_ZONE}
    @odoo      host odoo.${QA_ZONE}

    handle @hub {
        reverse_proxy hub:3000
    }
    handle @goldberry {
        reverse_proxy goldberry:3001
    }
    handle @ggg {
        reverse_proxy ggg:3001
    }
    handle @nursery {
        reverse_proxy nursery:3001
    }
    handle @odoo {
        reverse_proxy odoo:8069
    }

    handle {
        respond "Unknown QA host" 404
    }
}
