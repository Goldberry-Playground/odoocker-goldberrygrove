#cloud-config
# Grove Preview - bootstrap for per-PR droplet.
# Templated by Terraform's templatefile() in main.tf locals. Substituted
# variables: pr_number, preview_host, odoo_image_tag, frontend_image_tags,
# snapshot_date, spaces_access_key, spaces_secret_key, ghost_content_keys,
# do_token_for_caddy, compose_yml (read from compose/docker-compose.preview.yml),
# caddyfile_tpl (read from compose/Caddyfile.tpl).

package_update: true
package_upgrade: false # don't burn boot time on apt upgrade - base image is recent enough

packages:
  - ca-certificates
  - curl
  - gnupg
  - zstd
  - unzip

write_files:
  # 0644 not 0600: the file is bind-mounted read-only into the odoo
  # container (/.env) where odoorc.sh reads it as the non-root `odoo`
  # user - 0600 root:root is unreadable there and boots die on
  # "/odoorc.sh: line 62: .env: Permission denied". Same permission
  # qa-app-platform uses for its /etc/grove/.env. Proven live on the
  # PR #108 acceptance droplet (GOL-6, 2026-07-13).
  - path: /etc/grove/.env
    permissions: "0644"
    content: |
      POSTGRES_PASSWORD=__POSTGRES_PASSWORD__
      DO_API_TOKEN=${do_token_for_caddy}
      GHOST_KEY_GOLDBERRY=${ghost_content_keys["goldberry"]}
      GHOST_KEY_GGG=${ghost_content_keys["ggg"]}
      GHOST_KEY_NURSERY=${ghost_content_keys["nursery"]}

      # Consumed by odoorc.sh inside the odoo container (via the /.env
      # bind-mount in docker-compose.preview.yml) to render odoo.conf.
      # Without these the conf keeps literal $${DB_PORT} placeholders and
      # Odoo crash-loops. Mirrors qa-app-platform's /etc/grove/.env keys.
      DB_HOST=postgres
      DB_PORT=5432
      DB_NAME=grove_preview
      DB_USER=odoo
      DB_PASSWORD=__POSTGRES_PASSWORD__
      ODOO_ADMIN_PASSWORD=__ODOO_ADMIN_PASSWORD__
      # Only list dirs that EXIST in the image/mounts - Odoo refuses to
      # start on a nonexistent addons dir (2026-07-05 incident). No
      # git-sync sidecar in the preview stack yet, so /workspace/current
      # is the empty dir entrypoint.sh mkdirs.
      ADDONS_PATH=/usr/lib/python3/dist-packages/odoo/addons,/workspace/current

  - path: /etc/grove/Caddyfile
    content: |
      ${indent(6, replace(replace(caddyfile_tpl, "$${PREVIEW_HOST}", preview_host), "$${PREVIEW_ZONE}", "preview.gatheringatthegrove.com"))}

  - path: /etc/grove/docker-compose.yml
    content: |
      ${indent(6, replace(replace(replace(replace(replace(replace(replace(compose_yml, "{{ODOO_IMAGE_TAG}}", odoo_image_tag), "{{HUB_TAG}}", frontend_image_tags["hub"]), "{{GOLDBERRY_TAG}}", frontend_image_tags["goldberry"]), "{{GGG_TAG}}", frontend_image_tags["ggg"]), "{{NURSERY_TAG}}", frontend_image_tags["nursery"]), "{{PREVIEW_HOST}}", preview_host), "{{PREVIEW_ZONE}}", "preview.gatheringatthegrove.com"))}

  - path: /opt/grove/restore.sh
    permissions: "0755"
    content: |
      #!/usr/bin/env bash
      set -euo pipefail

      SNAP_DATE="${snapshot_date}"
      export AWS_ACCESS_KEY_ID="${spaces_access_key}"
      export AWS_SECRET_ACCESS_KEY="${spaces_secret_key}"
      ENDPOINT="https://nyc3.digitaloceanspaces.com"
      BUCKET="grove-preview-data"

      echo "[restore] pulling snapshot $${SNAP_DATE}"
      aws --endpoint-url "$${ENDPOINT}" s3 cp \
        "s3://$${BUCKET}/snapshots/prod-sanitized-$${SNAP_DATE}.sql.zst" /tmp/snap.sql.zst
      aws --endpoint-url "$${ENDPOINT}" s3 cp \
        "s3://$${BUCKET}/filestore/prod-sanitized-$${SNAP_DATE}.tar.zst" /tmp/filestore.tar.zst

      echo "[restore] starting postgres"
      cd /etc/grove
      docker compose --env-file /etc/grove/.env up -d postgres
      until docker compose exec -T postgres pg_isready -U odoo -d grove_preview; do
        sleep 2
      done

      echo "[restore] loading dump"
      zstd -d -c /tmp/snap.sql.zst \
        | docker compose exec -T postgres psql -U odoo -d grove_preview

      echo "[restore] running post-purge"
      cat <<'SQL' | docker compose exec -T postgres psql -U odoo -d grove_preview
      BEGIN;
      DELETE FROM payment_token;
      DELETE FROM res_partner_bank;
      DELETE FROM mail_notification;
      DELETE FROM bus_bus;
      COMMIT;
      SQL

      echo "[restore] extracting filestore"
      mkdir -p /var/lib/docker/volumes/grove_odoo_filestore/_data/grove_preview
      zstd -d -c /tmp/filestore.tar.zst | \
        tar -xf - -C /var/lib/docker/volumes/grove_odoo_filestore/_data/grove_preview --strip-components=1

      rm -f /tmp/snap.sql.zst /tmp/filestore.tar.zst

      echo "[restore] starting odoo (needed to mint the storefront API key)"
      # Bring odoo up FIRST, then mint via `docker compose exec` on the
      # running service. We can NOT mint with `docker compose run ... odoo
      # shell`: entrypoint.sh's APP_ENV=preview branch `exec`s the Odoo
      # HTTP server and IGNORES the `shell` subcommand, so the one-off
      # container never runs mint_key.py, never prints APIKEY, and hangs
      # forever serving /web/health (a server, not a shell). `exec` on the
      # already-running server spawns a SECOND process that honours `shell`
      # and reads mint_key.py from stdin. Proven live on the PR #108
      # acceptance droplet (GOL-6/GOL-344, 2026-07-13): the `run` form hung
      # indefinitely; an in-container `exec` minted a key first try.
      docker compose --env-file /etc/grove/.env up -d --wait odoo

      echo "[restore] minting storefront Odoo API key"
      # The storefronts (goldberry/ggg/nursery) fail closed at runtime if
      # ODOO_API_KEY is missing (tenant.secrets.ts requireEnv). Mint a single
      # global-scope (NULL) key against the restored DB and inject it into .env
      # before the frontends come up. grove_headless resolves the tenant from
      # the X-Grove-Tenant header, so one key serves all three. (GOL-344 Task 2)
      KEY=""
      for attempt in 1 2 3; do
        # `|| true`: this script runs under `set -euo pipefail`, so a failed
        # mint (nonzero -> pipefail) would otherwise abort the WHOLE restore
        # here - skipping the retry loop, the WARN fallback, and the frontend
        # `docker compose up`, leaving the storefronts down.
        KEY=$(docker compose --env-file /etc/grove/.env exec -T odoo \
                odoo shell -d grove_preview --no-http --logfile=/dev/null \
                < /opt/grove/mint_key.py 2>>/var/log/grove-mint.log \
              | sed -n 's/^APIKEY://p' | head -n1) || true
        [ -n "$KEY" ] && break
        echo "[restore] mint attempt $attempt failed; retrying in 10s"
        sleep 10
      done
      if [ -n "$KEY" ]; then
        sed -i '/^ODOO_API_KEY=/d' /etc/grove/.env # idempotent
        echo "ODOO_API_KEY=$KEY" >> /etc/grove/.env
        echo "[restore] ODOO_API_KEY minted and written to /etc/grove/.env"
      else
        echo "[restore] WARN: ODOO_API_KEY mint failed - storefronts will fail closed" >&2
      fi

      echo "[restore] starting full stack"
      docker compose --env-file /etc/grove/.env up -d

      echo "[restore] done"

  - path: /opt/grove/mint_key.py
    permissions: "0600"
    content: |
      # Mint a global-scope (NULL) Odoo API key for the preview storefronts and
      # print it once as "APIKEY:<key>". Fed to `odoo shell` over stdin (see
      # restore.sh). _generate is private, so this can't run over RPC - it must
      # execute in-process where `env` is bound. Mirrors the documented pattern
      # in skills/odoo-logistics/scripts/mint_logistics_key.py. NULL scope is
      # what Odoo 19 bearer auth requires (a NULL-scope key satisfies any scope
      # check); the admin-owned key is acceptable only because the preview
      # droplet is ephemeral with sanitized data. (GOL-344 Task 2)
      import inspect, sys

      KEY_NAME = "preview-storefront"
      SCOPE = None  # NULL scope - accepted for any scope by Odoo 19 bearer auth

      user = env.ref("base.user_admin")  # uid 2 - always present in any DB
      Apikeys = env["res.users.apikeys"].sudo()

      # Idempotent: revoke any prior key of this name (SQL delete - the ORM
      # unlink path is gated by an identity re-check we can't satisfy in a shell).
      prior = Apikeys.search([("user_id", "=", user.id), ("name", "=", KEY_NAME)])
      if prior:
          env.cr.execute("DELETE FROM res_users_apikeys WHERE id IN %s", (tuple(prior.ids),))
          Apikeys.invalidate_model()

      generate = env["res.users.apikeys"].with_user(user)._generate
      kwargs = {}
      if "expiration_date" in inspect.signature(generate).parameters:
          kwargs["expiration_date"] = False  # non-expiring
      key = generate(SCOPE, KEY_NAME, **kwargs)
      env.cr.commit()

      sys.stdout.write("APIKEY:" + key + "\n")
      sys.stdout.flush()

runcmd:
  # Install Docker (Ubuntu 24.04 noble) per docs.docker.com
  - install -m 0755 -d /etc/apt/keyrings
  - curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  - chmod a+r /etc/apt/keyrings/docker.asc
  - |
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
      > /etc/apt/sources.list.d/docker.list
  - apt-get update
  - apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  - systemctl enable --now docker

  # Generate the POSTGRES_PASSWORD now that openssl is available, and substitute
  # the placeholder in /etc/grove/.env (write_files runs before runcmd, so we
  # couldn't inline it earlier). Two lines carry the placeholder
  # (POSTGRES_PASSWORD for the postgres container, DB_PASSWORD for
  # odoorc.sh) - sed replaces once per line, covering both.
  - PGPW=$(openssl rand -hex 24) && sed -i "s|__POSTGRES_PASSWORD__|$PGPW|" /etc/grove/.env
  - OAPW=$(openssl rand -hex 16) && sed -i "s|__ODOO_ADMIN_PASSWORD__|$OAPW|" /etc/grove/.env

  # AWS CLI v2 via the official installer -- Ubuntu 24.04 (noble) dropped the
  # apt `awscli` package, so restore.sh's `aws s3 cp` needs this. Installs to
  # /usr/local/bin/aws.
  - curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
  - unzip -q -o /tmp/awscliv2.zip -d /tmp && /tmp/aws/install --update

  # Run restore + bring stack up. Logs to /var/log/grove-restore.log for triage.
  - /opt/grove/restore.sh > /var/log/grove-restore.log 2>&1

  # Health gate sentinel - the preview-up workflow polls for this file before
  # posting the URL to the PR (Task 3.3). Up to 5 minutes for the full stack
  # to be ready behind Caddy.
  - |
    for i in $(seq 1 60); do
      if curl -sf -o /dev/null -m 5 -k https://localhost/; then
        touch /var/lib/cloud/instance/grove-ready
        break
      fi
      sleep 5
    done
