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
}

# Apex + wildcard in one site block = ONE cert with two identifiers,
# requested once per Caddy /data lifetime (named volume; survives compose
# restart, dies with droplet -- but DNS-01 + 50-certs/week-per-domain
# means a fresh issue is cheap).
${QA_ZONE}, *.${QA_ZONE} {
    tls {
        dns digitalocean {env.DO_API_TOKEN}
    }

    log {
        output stdout
        format console
    }

    # Route by exact Host header to the right upstream container.
    # Tenant frontends in grove-sites listen on 3001; hub on 3000.
    @hub       host ${QA_ZONE}
    @goldberry host goldberry.${QA_ZONE}
    @ggg       host ggg.${QA_ZONE}
    @nursery   host nursery.${QA_ZONE}
    @odoo      host odoo.${QA_ZONE}

    handle @hub       { reverse_proxy hub:3000 }
    handle @goldberry { reverse_proxy goldberry:3001 }
    handle @ggg       { reverse_proxy ggg:3001 }
    handle @nursery   { reverse_proxy nursery:3001 }
    handle @odoo      { reverse_proxy odoo:8069 }

    handle {
        respond "Unknown QA host" 404
    }
}
