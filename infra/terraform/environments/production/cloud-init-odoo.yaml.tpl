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
#   spaces_*            : BUCKET-SCOPED Spaces key + endpoint for the nightly
#                         filestore backup (GOL-99) - never the plumbing key
#   backups_bucket      : grove-odoo-backups
#   healthchecks_ping_url : dead-man's switch for the filestore backup ONLY
#                         (separate check from the blogs backup)

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
  - rclone

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

  # -- Nightly filestore backup (GOL-99) --------------------------------------
  # rclone creds live HERE (0600, root-only), deliberately NOT in
  # /etc/grove/.env: that file is 0644 because the in-container `odoo` user has
  # to read it, so it stays credential-light. This key is bucket-scoped to
  # grove-odoo-backups (odoo.tf), so worst case it touches only backups.
  - path: /root/.config/rclone/rclone.conf
    permissions: "0600"
    content: |
      [spaces]
      type = s3
      provider = DigitalOcean
      access_key_id = ${spaces_access_id}
      secret_access_key = ${spaces_secret_key}
      endpoint = ${spaces_endpoint}
      acl = private

  - path: /usr/local/bin/grove-odoo-backup.sh
    permissions: "0755"
    content: |
      #!/usr/bin/env bash
      # Nightly Odoo filestore backup (GOL-99, GOL-382).
      #
      # Mirrors /mnt/odoo-filestore/filestore -> spaces:${backups_bucket}/filestore/current
      # Odoo's filestore is content-addressed (<db>/<2-char>/<sha1-of-content>)
      # and write-once, so an incremental sync moves only new attachments -
      # a nightly tar would re-upload the whole filestore every night.
      #
      # Restore: /usr/local/bin/grove-odoo-restore.sh (docs/RUNBOOK-odoo-filestore-restore.md)
      set -euo pipefail

      SRC=/mnt/odoo-filestore/filestore
      BUCKET="spaces:${backups_bucket}"
      STAMP=$(date -u +%Y-%m-%dT%H%M%SZ)

      # GUARD: the one failure that must never reach the mirror. If the block
      # volume did not mount, /mnt/odoo-filestore is an empty dir on the root
      # disk - syncing that would empty the mirror. Every other guard below is
      # defense in depth; this is the load-bearing one.
      if ! mountpoint -q /mnt/odoo-filestore; then
        echo "::error:: /mnt/odoo-filestore is not a mountpoint - refusing to sync (volume detached?)"
        exit 1
      fi

      # A fresh install has no filestore dir until Odoo writes its first
      # attachment. That is legitimate, not a failure: sync a no-op and ping.
      mkdir -p "$SRC"

      COUNT=$(find "$SRC" -type f | wc -l)
      BYTES=$(du -sb "$SRC" | cut -f1)

      # --backup-dir: sync's only destructive act is deleting. Everything it
      # would delete or overwrite lands in archive/ (35d lifecycle) instead of
      # vanishing, so a bad night is recoverable rather than replicated.
      # --max-delete: tripwire. Odoo's attachment GC deletes a trickle; a
      # mass deletion means something is wrong and should fail loudly.
      rclone sync "$SRC" "$BUCKET/filestore/current" \
        --backup-dir "$BUCKET/filestore/archive/$STAMP" \
        --max-delete 1000 \
        --s3-no-check-bucket --fast-list \
        --transfers 8 --checkers 16 \
        --stats-one-line --stats 5m

      # Manifest = what a restore verifies against. Without it "the backup ran"
      # and "the backup is complete" are indistinguishable.
      echo "{\"stamp\":\"$STAMP\",\"files\":$COUNT,\"bytes\":$BYTES}" > /tmp/manifest.json
      rclone copyto /tmp/manifest.json "$BUCKET/filestore/manifest/$STAMP.json" --s3-no-check-bucket
      rm -f /tmp/manifest.json

      # Ping LAST and only on success: set -euo pipefail means any failure above
      # skips this, and the dead-man's switch fires. A backup nobody is watching
      # is not a backup.
      if [ -n "${healthchecks_ping_url}" ]; then
        curl -fsS -m 10 --retry 3 "${healthchecks_ping_url}" > /dev/null
      fi
      echo "[ok] odoo filestore backup $STAMP synced ($COUNT files, $BYTES bytes)"

  - path: /usr/local/bin/grove-odoo-restore.sh
    permissions: "0755"
    content: |
      #!/usr/bin/env bash
      # Restore the Odoo filestore from the Spaces mirror (GOL-382 step 3).
      #
      #   grove-odoo-restore.sh            # restore from filestore/current
      #   grove-odoo-restore.sh --verify   # dry-run: report drift, change nothing
      #
      # This script is the reason the backup counts as a backup. It ships WITH
      # the droplet so a restore never depends on reconstructing a procedure
      # from memory during an incident.
      set -euo pipefail

      SRC=/mnt/odoo-filestore/filestore
      BUCKET="spaces:${backups_bucket}"
      MODE="$${1:-restore}"

      if ! mountpoint -q /mnt/odoo-filestore; then
        echo "::error:: /mnt/odoo-filestore is not a mountpoint - attach the volume first"
        exit 1
      fi

      LATEST=$(rclone lsf "$BUCKET/filestore/manifest/" --s3-no-check-bucket | sort | tail -1)
      echo "[info] latest manifest: $${LATEST:-<none>}"
      if [ -n "$LATEST" ]; then
        rclone cat "$BUCKET/filestore/manifest/$LATEST" --s3-no-check-bucket
      fi

      if [ "$MODE" = "--verify" ]; then
        # check: compares hashes/sizes both ways and exits non-zero on any
        # difference. This is the acceptance test for "the backup is restorable".
        rclone check "$BUCKET/filestore/current" "$SRC" --s3-no-check-bucket --fast-list
        echo "[ok] mirror matches the live filestore"
        exit 0
      fi

      # Stop Odoo first: restoring under a running server races the attachment
      # writer and can leave ir.attachment rows pointing at files not yet back.
      #
      # `cd` on its own line, NOT `cd /etc/grove && docker compose ...`: in an
      # `A && B` list, bash exempts A's failure from `set -e`, so a failed cd
      # would skip the stop, fall through, and copy underneath a RUNNING Odoo -
      # the exact race this stop exists to prevent. A bare cd is not exempt.
      cd /etc/grove
      # `|| true` is scoped to compose only: stopping an already-stopped Odoo
      # is a fine state to restore from.
      docker compose --env-file /etc/grove/.env stop odoo || true

      rclone copy "$BUCKET/filestore/current" "$SRC" \
        --s3-no-check-bucket --fast-list --transfers 8 --checkers 16 --stats-one-line

      # Ownership must match the container's odoo user or every attachment read
      # 500s. Resolve from the image - it is 100:101 on odoo:19, NOT 101:101
      # (the bug that bit GOL-93/GOL-105; see CLAUDE.md deploy invariants).
      . /etc/grove/.env
      IMG="ghcr.io/goldberry-playground/grove-odoo:$${ODOO_TAG:-latest}"
      OUID=$(docker run --rm --entrypoint id "$IMG" -u odoo 2>/dev/null || echo 100)
      OGID=$(docker run --rm --entrypoint id "$IMG" -g odoo 2>/dev/null || echo 101)
      chown -R "$OUID:$OGID" /mnt/odoo-filestore

      cd /etc/grove
      docker compose --env-file /etc/grove/.env start odoo
      echo "[ok] filestore restored from $BUCKET/filestore/current; Odoo restarted"

  - path: /etc/cron.d/grove-odoo-backup
    permissions: "0644"
    content: |
      # 03:00 UTC - ahead of the blogs backup (03:30) so the two large Spaces
      # uploads do not contend.
      0 3 * * * root /usr/local/bin/grove-odoo-backup.sh >> /var/log/grove-odoo-backup.log 2>&1

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

  # -- Make the durable filestore writable by the container's `odoo` user, else
  # attachment writes 500. Resolve uid:gid FROM THE IMAGE, not hardcoded: the
  # official odoo:19 `odoo` user is 100:101, NOT 101:101 (verified 2026-07-09
  # via `docker exec ... id odoo`); GOL-93 shipped 101:101, which would leave the
  # fresh volume unwritable. The probe pulls grove-odoo (compose-up reuses it);
  # the 100:101 fallback is correct if the probe fails. The mounts: module has
  # already mounted LABEL=filestore by the time runcmd fires.
  - |
    . /etc/grove/.env
    IMG="ghcr.io/goldberry-playground/grove-odoo:$${ODOO_TAG:-latest}"
    OUID=$(docker run --rm --entrypoint id "$IMG" -u odoo 2>/dev/null || echo 100)
    OGID=$(docker run --rm --entrypoint id "$IMG" -g odoo 2>/dev/null || echo 101)
    mkdir -p /mnt/odoo-filestore
    chown -R "$OUID:$OGID" /mnt/odoo-filestore

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
