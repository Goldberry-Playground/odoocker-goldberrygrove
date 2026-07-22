#cloud-config

# Templated variables (resolved by TF templatefile()):
#   qa_zone             : qa-l3.gatheringatthegrove.com
#   odoo_image_tag      : grove-odoo image tag
#   caddy_image_tag     : grove-caddy image tag
#   pg_host             : Managed PG private hostname
#   pg_port             : Managed PG port (usually 25060)
#   pg_database         : Odoo database name in Managed PG
#   pg_user             : Odoo DB user
#   pg_password         : Odoo DB user password (sensitive)
#   do_token_for_caddy  : DO API token for DNS-01 ACME
#   acme_endpoint       : LE prod or staging directory URL
#   compose_yml_b64     : base64 of compose/docker-compose.qa.yml
#   caddyfile_tpl_b64   : base64 of compose/Caddyfile.tpl (with $${QA_ZONE} pre-substituted)

ssh_pwauth: false

apt:
  conf: |
    APT::Get::Assume-Yes "true";
    DPkg::Options:: "--force-confnew";
    DPkg::Options:: "--force-confdef";

packages:
  - ca-certificates
  - curl
  - gnupg
  - jq
  - postgresql-client       # smoke-test connectivity to Managed PG (psql)
  - lsb-release

# Persistent volumes - same pattern as monolith QA (ADR-005 PR-A).
# Bind via LABEL so the device name doesn't matter and re-attaches survive
# reboot. The systemd mount unit options retry on transient failures.
#   - LABEL=data      -> Caddy /data (LE certs + ACME account)
#   - LABEL=filestore -> Odoo filestore /var/lib/odoo (product photos +
#     ir.attachment binaries). DISTINCT label so LABEL= mounts stay
#     unambiguous with both volumes attached. Survives droplet replace so a
#     recreate never wipes product photos (GOL-93).
mounts:
  - ["LABEL=data", "/mnt/caddy-data", "ext4", "defaults,nofail,noatime,discard,x-systemd.device-timeout=120,x-systemd.mount-timeout=30", "0", "2"]
  - ["LABEL=filestore", "/mnt/odoo-filestore", "ext4", "defaults,nofail,noatime,discard,x-systemd.device-timeout=120,x-systemd.mount-timeout=30", "0", "2"]

write_files:
  # /etc/grove/.env - consumed by docker compose. Mode 0644 (not 0600)
  # because grove-odoo's entrypoint.sh + odoorc.sh run as the non-root
  # `odoo` user inside the container and need to read this file to
  # substitute placeholders into /etc/odoo/odoo.conf. Same documented
  # rationale as monolith QA's cloud-init.yaml.tpl.
  - path: /etc/grove/.env
    permissions: "0644"
    content: |
      # Image tags
      ODOO_TAG=${odoo_image_tag}
      CADDY_TAG=${caddy_image_tag}

      # Managed PG connection (private network from this droplet)
      DB_HOST=${pg_host}
      DB_PORT=${pg_port}
      DB_NAME=${pg_database}
      DB_USER=${pg_user}
      DB_PASSWORD=${pg_password}

      # Odoo admin (master password for DB management endpoints).
      # Randomized at boot via the runcmd step below; placeholder here.
      ODOO_ADMIN_PASSWORD=__ODOO_ADMIN_PASSWORD__

      # Addons path substituted into odoo.conf by odoorc.sh at container
      # start. /workspace/current is populated by the custom-modules-sync
      # git-sync sidecar (grove-odoo-modules repo; grove_headless lives
      # there). Only list dirs that EXIST in the image + mounts -- Odoo
      # refuses to start on a nonexistent addons dir. Without this var the
      # placeholder substitutes to empty and Odoo silently runs with stock
      # community addons only (how the 2026-07-05 "module not in list"
      # incident happened).
      ADDONS_PATH=/usr/lib/python3/dist-packages/odoo/addons,/workspace/current

      # Caddy DNS-01 ACME - single hostname (odoo.${qa_zone}), so the
      # rate-limit class that motivated the monolith's multi-issuer
      # fallback (ADR-005 PR-D) is essentially non-applicable here.
      DO_API_TOKEN=${do_token_for_caddy}
      ACME_CA=${acme_endpoint}

      # Stripe TEST-mode keys (sandbox checkout — GOL-688/696). LOWERCASE
      # names on purpose: grove_headless controllers/main.py reads them via
      # os.environ.get("stripe_test_secret_key") / ...("stripe_test_webhook_secret").
      # entrypoint.sh + odoorc.sh export every KEY=VALUE line here into the
      # odoo process env, so these flow straight through. Empty until the
      # CI/TF apply op account can read the grove-qa 1P vault (GOL-696) --
      # grove_headless defaults to "" so an empty value is a safe no-op.
      stripe_test_secret_key=${stripe_test_secret_key}
      stripe_test_webhook_secret=${stripe_test_webhook_secret}

  # Compose YAML - base64-encoded so cloud-init's YAML parser never sees
  # its content (avoids the embedded-block-scalar parse failures we hit
  # repeatedly on the monolith).
  - path: /etc/grove/docker-compose.yml
    permissions: "0644"
    encoding: b64
    content: ${compose_yml_b64}

  # Caddyfile (already templated by TF for $${QA_ZONE} -> qa-l3.<apex>)
  - path: /etc/grove/Caddyfile
    permissions: "0644"
    encoding: b64
    content: ${caddyfile_tpl_b64}

runcmd:
  # -- Docker install (official convenience script - same approach as monolith)
  - curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  - sh /tmp/get-docker.sh
  - usermod -aG docker root
  - systemctl enable docker
  - systemctl start docker

  # -- Randomize Odoo admin password (sed-substitute the placeholder)
  - OAPW=$(openssl rand -hex 16) && sed -i "s|__ODOO_ADMIN_PASSWORD__|$OAPW|" /etc/grove/.env

  # -- Smoke-test Managed PG connectivity BEFORE starting the stack.
  # `pg_isready` returns 0 only if PG is accepting connections. The Managed
  # PG cluster is provisioned by TF before this droplet is created, so it
  # SHOULD be ready instantly - but cluster state lags resource creation by
  # ~1-2 min sometimes. Loop with timeout.
  - |
    . /etc/grove/.env
    for i in $(seq 1 60); do
      if pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t 5 >/dev/null 2>&1; then
        echo "[ok] Managed PG ready after $i polls" | tee -a /var/log/grove-qa-l3-up.log
        break
      fi
      [ $i -eq 60 ] && { echo "::error::Managed PG not ready after 5 min"; exit 1; }
      sleep 5
    done

  # -- Make the durable filestore volume writable by the container's `odoo`
  #    user, or Odoo can't write attachments (product-image 500s). Resolve
  #    uid:gid FROM THE IMAGE rather than hardcoding: the official odoo:19
  #    `odoo` user is 100:101, NOT 101:101 (verified 2026-07-09 via
  #    `docker exec grove-odoo-1 id odoo`). GOL-93 shipped a hardcoded 101:101,
  #    which would leave the fresh volume unwritable. The `docker run ... id`
  #    probe pulls grove-odoo, which the compose-up below reuses; the fallback
  #    (100:101) is the correct value if the probe ever fails. The `mounts:`
  #    module has already mounted LABEL=filestore at /mnt/odoo-filestore.
  - |
    . /etc/grove/.env
    IMG="ghcr.io/goldberry-playground/grove-odoo:$${ODOO_TAG:-latest}"
    OUID=$(docker run --rm --entrypoint id "$IMG" -u odoo 2>/dev/null || echo 100)
    OGID=$(docker run --rm --entrypoint id "$IMG" -g odoo 2>/dev/null || echo 101)
    mkdir -p /mnt/odoo-filestore
    chown -R "$OUID:$OGID" /mnt/odoo-filestore

  # -- Bring up the compose stack
  - cd /etc/grove && docker compose --env-file /etc/grove/.env up -d > /var/log/grove-qa-l3-up.log 2>&1

  # -- Grove-ready sentinel - touched once Caddy is serving 200/303 on /.
  # qa-deploy-l3 workflow polls this file via SSH (same pattern as monolith
  # qa-deploy.yml). Touched only if the sentinel curl succeeds, so the
  # workflow can detect failures by sentinel absence + log inspection.
  - |
    for i in $(seq 1 60); do
      code=$(curl -sk -o /dev/null -w '%%{http_code}' --max-time 5 "https://odoo.${qa_zone}/" || echo 000)
      if [ "$code" = "200" ] || [ "$code" = "303" ] || [ "$code" = "302" ]; then
        touch /var/lib/cloud/instance/grove-ready
        echo "[ok] grove-ready sentinel touched (HTTP $code)" >> /var/log/grove-qa-l3-up.log
        exit 0
      fi
      sleep 5
    done
    echo "::error::Odoo never came online at https://odoo.${qa_zone}/" >> /var/log/grove-qa-l3-up.log
