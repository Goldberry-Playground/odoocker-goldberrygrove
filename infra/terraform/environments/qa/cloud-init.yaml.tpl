#cloud-config
# Grove QA -- droplet bootstrap. Runs once on first boot. To force a re-run
# (e.g., to pick up new image tags), re-create the droplet via
# `terraform taint digitalocean_droplet.qa` + `terraform apply`.
#
# Templated by TF templatefile() in main.tf. Substituted variables:
#   qa_zone             -- qa.gatheringatthegrove.com
#   odoo_image_tag      -- grove-odoo image tag
#   frontend_image_tags -- map per frontend
#   ghost_key_goldberry -- Content API key (may be empty)
#   compose_yml_b64     -- entire docker-compose.qa.yml, base64-encoded
#   caddyfile_tpl_b64   -- entire Caddyfile.tpl with QA_ZONE substituted, base64-encoded
#
# Why base64 for the embedded files: the previous design embedded them as
# raw YAML block scalars via indent() interpolation. That hit a YAML parse
# failure THREE separate times on 2026-06-24:
#   PR #62: em-dashes in comments (0x80 byte rejected by PyYAML)
#   PR #65: $$ vs $ substitution leftover (stray dollar sign in output)
#   PR #66: missing 6-space prefix before indent() call broke block scalar
# Base64 sidesteps the entire class -- cloud-init's parser never sees the
# embedded content, just an opaque blob. Per DO cloud-config tutorial.
#
# (Note: do NOT use the literal $ { ... } syntax in comments here -- TF
# templatefile() will try to evaluate it as an expression, which we just
# spent 30 sec discovering the hard way.)
#
# SECURITY: Anything in cloud-init's user_data is readable by every user on
# the droplet (per DO cloud-config docs). DO NOT add credentials here. The
# /etc/grove/.env file is mode 0600 (root-only) but the user-data blob in
# /var/lib/cloud/instance/user-data.txt may be wider. POSTGRES + ODOO admin
# passwords are GENERATED on the droplet via openssl (runcmd, below), not
# embedded. Any future credential additions should follow that pattern.

package_update: true
package_upgrade: false

packages:
  - ca-certificates
  - curl
  - gnupg

write_files:
  # Env file consumed by docker compose
  - path: /etc/grove/.env
    # Mode 0644 (not 0600) is intentional: the file is bind-mounted into the
    # grove-odoo container at /.env, where /entrypoint.sh + /odoorc.sh run as
    # the `odoo` user (not root) and need to read it to substitute the
    # placeholders in /etc/odoo/odoo.conf. With 0600 the bind-mount succeeds
    # but odoorc.sh hits "Permission denied" and Odoo crashes on the
    # un-substituted port-option string.
    # (Note: do NOT write the placeholders with $ { } syntax in these
    # comments -- TF templatefile() evaluates them. Learned three times.)
    #
    # Security: the loss of defense-in-depth is theoretical on this droplet:
    # (a) no non-root human users exist on the QA droplet; (b) the bind-mount
    # is ONLY on the odoo service, so other containers can't read it via
    # /.env; (c) the SAME values are passed via compose's `environment:`
    # block to the odoo container's env anyway. 0644 doesn't widen the actual
    # exposure vs 0600.
    permissions: "0644"
    content: |
      POSTGRES_PASSWORD=__POSTGRES_PASSWORD__
      ODOO_ADMIN_PASSWORD=__ODOO_ADMIN_PASSWORD__
      # Postgres connection -- consumed by the odoo container's DB_PORT,
      # DB_HOST, DB_USER env substitutions in docker-compose. Defaults
      # because these were missing from the .env on 2026-06-24, causing
      # Odoo to crash with "invalid integer value" for the port option.
      # (Note: do NOT write the variables with $ { } syntax in these
      # comments -- TF templatefile() evaluates them. Learned this twice
      # tonight, once in PR #68 and now this PR.)
      DB_HOST=postgres
      DB_PORT=5432
      DB_USER=odoo
      QA_ZONE=${qa_zone}
      GHOST_KEY_GOLDBERRY=${ghost_key_goldberry}
      ODOO_IMAGE_TAG=${odoo_image_tag}
      HUB_TAG=${frontend_image_tags["hub"]}
      GOLDBERRY_TAG=${frontend_image_tags["goldberry"]}
      GGG_TAG=${frontend_image_tags["ggg"]}
      NURSERY_TAG=${frontend_image_tags["nursery"]}

  - path: /etc/grove/Caddyfile
    encoding: b64
    content: ${caddyfile_tpl_b64}

  - path: /etc/grove/docker-compose.yml
    encoding: b64
    content: ${compose_yml_b64}

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
  # Up to 10 minutes for the full stack + cert provisioning.
  #
  # Previously polled https://localhost/, but Caddy only has vhost blocks for
  # the public hostnames (qa.gatheringatthegrove.com, etc.) -- a localhost
  # request matched nothing and never returned 2xx, so the sentinel never
  # fired even when the stack was perfectly healthy. Now poll the actual
  # public apex URL: the droplet resolves it via the DO domain DNS pointing
  # to its own IP, and Caddy serves the hub vhost over the LE cert. A 2xx
  # response means TLS works, hub is up, and the world can reach it.
  - |
    for i in $(seq 1 120); do
      if curl -sf -o /dev/null -m 5 "https://${qa_zone}/"; then
        touch /var/lib/cloud/instance/grove-ready
        break
      fi
      sleep 5
    done
