# Grove QA -- Caddyfile template.
#
# Substituted at TF apply time (NOT at cloud-init time) via:
#   replace(caddyfile_tpl, "$${QA_ZONE}", qa_zone)
# inside infra/terraform/environments/qa/cloud-init.yaml.tpl. The pattern
# this file uses is SINGLE-dollar (${QA_ZONE}) because the replace() search
# string "$${QA_ZONE}" becomes the literal "${QA_ZONE}" per TF template
# escaping (a $$ collapses to a $). An earlier version of this file
# mistakenly used $${QA_ZONE} which only got partially replaced, producing
# a stray $ in the rendered Caddyfile and a YAML parse failure on the
# droplet. Don't "escape" the ${QA_ZONE} below -- leave them single-dollar.
#
# Routes by Host header to the right upstream container. TLS via Let's
# Encrypt HTTP-01 challenge (port 80 open in firewall). DO DNS-01 wildcard
# is a follow-up if we ever want to avoid the port-80 dependency.
#
# All upstreams are on the default Docker bridge network -- Caddy resolves
# them by service name (hub, goldberry, ggg, nursery, odoo).

# Hub -- qa.gatheringatthegrove.com (apex of the delegated zone)
${QA_ZONE} {
    reverse_proxy hub:3000
    log {
        output stdout
        format console
    }
}

# Goldberry storefront
goldberry.${QA_ZONE} {
    reverse_proxy goldberry:3000
}

# GGG (woodworking) storefront
ggg.${QA_ZONE} {
    reverse_proxy ggg:3000
}

# Nursery storefront
nursery.${QA_ZONE} {
    reverse_proxy nursery:3000
}

# Odoo admin -- same as the others but proxied to Odoo's 8069 port.
# Auth gate is Odoo's login screen; no additional access control here.
odoo.${QA_ZONE} {
    reverse_proxy odoo:8069
}
