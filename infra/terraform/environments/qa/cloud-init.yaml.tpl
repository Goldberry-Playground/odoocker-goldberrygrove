#cloud-config
# Grove QA -- droplet bootstrap. Runs once on first boot. To force a re-run
# (e.g., to pick up new image tags), re-create the droplet via
# `terraform taint digitalocean_droplet.qa` + `terraform apply`.
#
# Templated by TF templatefile() in main.tf. Substituted variables:
#   qa_zone             -- qa.gatheringatthegrove.com
#   do_token_for_caddy  -- for Caddy's DO DNS-01 ACME challenge
#   odoo_image_tag      -- grove-odoo image tag
#   frontend_image_tags -- map per frontend
#   ghost_key_goldberry -- Content API key (may be empty)
#   compose_yml         -- entire docker-compose.qa.yml as a string
#   caddyfile_tpl       -- entire Caddyfile.tpl as a string

package_update: true
package_upgrade: false

packages:
  - ca-certificates
  - curl
  - gnupg

write_files:
  # Env file consumed by docker compose
  - path: /etc/grove/.env
    permissions: "0600"
    content: |
      POSTGRES_PASSWORD=__POSTGRES_PASSWORD__
      ODOO_ADMIN_PASSWORD=__ODOO_ADMIN_PASSWORD__
      DO_API_TOKEN=${do_token_for_caddy}
      QA_ZONE=${qa_zone}
      GHOST_KEY_GOLDBERRY=${ghost_key_goldberry}
      ODOO_IMAGE_TAG=${odoo_image_tag}
      HUB_TAG=${frontend_image_tags["hub"]}
      GOLDBERRY_TAG=${frontend_image_tags["goldberry"]}
      GGG_TAG=${frontend_image_tags["ggg"]}
      NURSERY_TAG=${frontend_image_tags["nursery"]}

  - path: /etc/grove/Caddyfile
    content: |
${indent(6, replace(caddyfile_tpl, "$${QA_ZONE}", qa_zone))}

  - path: /etc/grove/docker-compose.yml
    content: |
${indent(6, compose_yml)}

runcmd:
  # Install Docker per docs.docker.com (Ubuntu noble = 24.04)
  - install -m 0755 -d /etc/apt/keyrings
  - curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  - chmod a+r /etc/apt/keyrings/docker.asc
  - |
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
      > /etc/apt/sources.list.d/docker.list
  - apt-get update
  - apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  - systemctl enable --now docker

  # Generate strong random passwords now that openssl is available (write_files
  # ran before runcmd; sed-substitute the placeholders in /etc/grove/.env).
  - PGPW=$(openssl rand -hex 24) && sed -i "s|__POSTGRES_PASSWORD__|$PGPW|" /etc/grove/.env
  - OAPW=$(openssl rand -hex 16) && sed -i "s|__ODOO_ADMIN_PASSWORD__|$OAPW|" /etc/grove/.env

  # Bring the stack up. Logs to /var/log/grove-qa-up.log for triage.
  - cd /etc/grove && docker compose --env-file /etc/grove/.env up -d > /var/log/grove-qa-up.log 2>&1

  # Health sentinel -- qa-deploy.yml polls for this file before posting URLs.
  # Up to 10 minutes for the full stack + cert provisioning (Caddy's first
  # DNS-01 challenge can take a few minutes).
  - |
    for i in $(seq 1 120); do
      if curl -sf -o /dev/null -m 5 -k https://localhost/; then
        touch /var/lib/cloud/instance/grove-ready
        break
      fi
      sleep 5
    done
