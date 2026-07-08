#cloud-config

# Cloud-init for the Level 3 PRODUCTION Odoo droplet (ADR-007 Phase 6, GOL-105).
#
# Copied from the validated QA L3 cloud-init (environments/qa-app-platform/
# cloud-init.yaml.tpl). Divergences from QA (all because prod Odoo is
# Cloudflare-proxied and terminates TLS with Origin CA cert FILES, not ACME):
#   - No DO_API_TOKEN / ACME_CA env (Caddy does no DNS-01 challenge).
#   - Writes the CF Origin CA cert + key to /etc/grove/certs (Caddy mounts them).
#   - Only ONE persistent volume (LABEL=filestore); no caddy-data ACME volume.
#
# Templated variables (resolved by TF templatefile()):
#   odoo_zone           : gatheringatthegrove.com  (Odoo host is odoo.<odoo_zone>)
#   odoo_image_tag      : grove-odoo image tag
#   caddy_tag           : official caddy image tag
#   pg_host/port/...    : Managed PG connection (private VPC)
#   origin_cert/key     : CF Origin CA cert + key for the hub zone (wildcard)
#   compose_yml_b64     : base64 of compose/docker-compose.odoo.yml
#   caddyfile_b64       : base64 of compose/Caddyfile-odoo.tpl

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
  - postgresql-client
  - lsb-release

# Durable filestore volume (GOL-93). Bind via LABEL so the device name doesn't
# matter and re-attaches survive reboot; survives a droplet replace so a
# recreate never wipes product photos.
mounts:
  - ["LABEL=filestore", "/mnt/odoo-filestore", "ext4", "defaults,nofail,noatime,discard,x-systemd.device-timeout=120,x-systemd.mount-timeout=30", "0", "2"]

write_files:
  # /etc/grove/.env - consumed by docker compose. Mode 0644 (not 0600) because
  # grove-odoo's entrypoint.sh + odoorc.sh run as the non-root `odoo` user
  # inside the container and must read this to substitute placeholders into
  # /etc/odoo/odoo.conf. Same rationale as QA L3. Keep it credential-LIGHT:
  # the DB password is here (unavoidable), but no Spaces/API keys.
  - path: /etc/grove/.env
    permissions: "0644"
    content: |
      # Image tags
      ODOO_TAG=${odoo_image_tag}
      CADDY_TAG=${caddy_tag}

      # Managed PG connection (private VPC network from this droplet)
      DB_HOST=${pg_host}
      DB_PORT=${pg_port}
      DB_NAME=${pg_database}
      DB_USER=${pg_user}
      DB_PASSWORD=${pg_password}

      # Odoo master password for DB-management endpoints. Randomized at boot
      # via the runcmd step below; placeholder here.
      ODOO_ADMIN_PASSWORD=__ODOO_ADMIN_PASSWORD__

      # Addons path substituted into odoo.conf by odoorc.sh at container start.
      # /workspace/current is populated by the custom-modules-sync git-sync
      # sidecar (grove-odoo-modules; grove_headless lives there). Without this
      # var Odoo silently runs stock community addons only.
      ADDONS_PATH=/usr/lib/python3/dist-packages/odoo/addons,/workspace/current

  # Compose YAML - base64 so cloud-init's YAML parser never sees its content.
  - path: /etc/grove/docker-compose.yml
    permissions: "0644"
    encoding: b64
    content: ${compose_yml_b64}

  # Caddyfile (static prod hostname; no TF substitution needed).
  - path: /etc/grove/Caddyfile
    permissions: "0644"
    encoding: b64
    content: ${caddyfile_b64}

  # CF Origin CA cert + key (hub zone; wildcard SAN covers odoo.<zone>).
  # Filename matches the Caddyfile's `tls /certs/<zone>.pem <zone>.key`.
  - path: /etc/grove/certs/${odoo_zone}.pem
    permissions: "0644"
    encoding: b64
    content: ${base64encode(origin_cert)}

  - path: /etc/grove/certs/${odoo_zone}.key
    permissions: "0600"
    encoding: b64
    content: ${base64encode(origin_key)}

runcmd:
  # -- Docker install (official convenience script - same as every Grove droplet)
  - curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  - sh /tmp/get-docker.sh
  - usermod -aG docker root
  - systemctl enable docker
  - systemctl start docker

  # -- Randomize Odoo admin password (sed-substitute the placeholder)
  - OAPW=$(openssl rand -hex 16) && sed -i "s|__ODOO_ADMIN_PASSWORD__|$OAPW|" /etc/grove/.env

  # -- Smoke-test Managed PG connectivity BEFORE starting the stack. Cluster
  # state can lag resource creation by ~1-2 min; loop with timeout.
  - |
    . /etc/grove/.env
    for i in $(seq 1 60); do
      if pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -t 5 >/dev/null 2>&1; then
        echo "[ok] Managed PG ready after $i polls" | tee -a /var/log/grove-prod-odoo-up.log
        break
      fi
      [ $i -eq 60 ] && { echo "::error::Managed PG not ready after 5 min"; exit 1; }
      sleep 5
    done

  # -- Make the durable filestore writable by the container's `odoo` user
  # (official odoo image uid:gid 101:101), else attachment writes 500. The
  # mounts: module has mounted LABEL=filestore by the time runcmd fires. GOL-93.
  - mkdir -p /mnt/odoo-filestore && chown -R 101:101 /mnt/odoo-filestore

  # -- Bring up the compose stack
  - cd /etc/grove && docker compose --env-file /etc/grove/.env up -d > /var/log/grove-prod-odoo-up.log 2>&1

  # -- Grove-ready sentinel. Probe Caddy LOCALLY (--resolve to 127.0.0.1) so
  # readiness doesn't depend on the Cloudflare-proxied DNS record having
  # propagated yet - it tests Caddy + the Origin CA cert + Odoo end to end.
  - |
    for i in $(seq 1 60); do
      code=$(curl -sk --resolve odoo.${odoo_zone}:443:127.0.0.1 -o /dev/null -w '%%{http_code}' --max-time 5 "https://odoo.${odoo_zone}/" || echo 000)
      if [ "$code" = "200" ] || [ "$code" = "303" ] || [ "$code" = "302" ]; then
        touch /var/lib/cloud/instance/grove-ready
        echo "[ok] grove-ready sentinel touched (HTTP $code)" >> /var/log/grove-prod-odoo-up.log
        exit 0
      fi
      sleep 5
    done
    echo "::error::Odoo never came online at https://odoo.${odoo_zone}/" >> /var/log/grove-prod-odoo-up.log
    exit 1
