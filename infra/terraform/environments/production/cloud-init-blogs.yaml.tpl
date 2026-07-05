#cloud-config

# Cloud-init for the production blogs droplet (Track 1, Grove Production Launch).
#
# Brings up: Docker, MySQL 8, 4x Ghost 6, Caddy (CF Origin CA TLS).
# All blog state on the attached DO volume at /mnt/blogs-data.
#
# Secrets randomized at first boot (never hardcoded):
#   MYSQL_ROOT_PASSWORD, MYSQL_GHOST_{HUB,GOLDBERRY,GGG,NURSERY}_PASSWORD
# Operator reads them from /etc/grove-blogs/.env (root-only, 0600).
#
# Nightly backup (03:30 UTC): mysqldump per DB + tar of each Ghost content
# dir -> rclone to Spaces daily/ prefix; 1st of month also copies to
# monthly/. Pings Healthchecks on success (dead-man's switch).

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
  - lsb-release
  - rclone

write_files:
  - path: /etc/grove-blogs/.env
    permissions: "0600"
    content: |
      GHOST_TAG=${ghost_tag}
      MYSQL_TAG=${mysql_tag}
      CADDY_TAG=${caddy_tag}

      GHOST_HUB_URL=${ghost_urls.hub}
      GHOST_GOLDBERRY_URL=${ghost_urls.goldberry}
      GHOST_GGG_URL=${ghost_urls.ggg}
      GHOST_NURSERY_URL=${ghost_urls.nursery}

      MYSQL_ROOT_PASSWORD=__MYSQL_ROOT_PASSWORD__
      MYSQL_GHOST_HUB_PASSWORD=__MYSQL_GHOST_HUB_PASSWORD__
      MYSQL_GHOST_GOLDBERRY_PASSWORD=__MYSQL_GHOST_GOLDBERRY_PASSWORD__
      MYSQL_GHOST_GGG_PASSWORD=__MYSQL_GHOST_GGG_PASSWORD__
      MYSQL_GHOST_NURSERY_PASSWORD=__MYSQL_GHOST_NURSERY_PASSWORD__

  - path: /etc/grove-blogs/docker-compose.yml
    permissions: "0644"
    encoding: b64
    content: ${compose_yml_b64}

  - path: /etc/grove-blogs/Caddyfile
    permissions: "0644"
    encoding: b64
    content: ${caddyfile_b64}

  - path: /etc/grove-blogs/mysql-init.sql.tpl
    permissions: "0600"
    encoding: b64
    content: ${mysql_init_b64}

%{ for zone, pair in origin_certs ~}
  - path: /etc/grove-blogs/certs/${zone}.pem
    permissions: "0644"
    encoding: b64
    content: ${base64encode(pair.cert)}

  - path: /etc/grove-blogs/certs/${zone}.key
    permissions: "0600"
    encoding: b64
    content: ${base64encode(pair.key)}

%{ endfor ~}
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

  - path: /usr/local/bin/grove-blogs-backup.sh
    permissions: "0755"
    content: |
      #!/usr/bin/env bash
      # Nightly blogs backup: mysqldump per tenant DB + tar of Ghost content
      # dirs -> Spaces. daily/ prefix expires via bucket lifecycle (35d);
      # 1st-of-month copies to monthly/ (no expiry).
      set -euo pipefail
      . /etc/grove-blogs/.env
      STAMP=$(date -u +%F)
      WORK=$(mktemp -d)
      trap 'rm -rf "$WORK"' EXIT

      for T in hub goldberry ggg nursery; do
        docker exec grove-blogs-mysql-1 sh -c \
          "exec mysqldump -uroot -p\"$MYSQL_ROOT_PASSWORD\" --single-transaction --routines ghost_$T" \
          > "$WORK/ghost_$T.sql"
        tar -czf "$WORK/ghost_$T-content.tar.gz" -C /mnt/blogs-data "ghost-$T"
      done

      rclone copy "$WORK" "spaces:${backups_bucket}/daily/$STAMP/" --s3-no-check-bucket
      if [ "$(date -u +%d)" = "01" ]; then
        rclone copy "$WORK" "spaces:${backups_bucket}/monthly/$STAMP/" --s3-no-check-bucket
      fi

      if [ -n "${healthchecks_ping_url}" ]; then
        curl -fsS -m 10 --retry 3 "${healthchecks_ping_url}" > /dev/null
      fi
      echo "[ok] blogs backup $STAMP uploaded"

  - path: /etc/cron.d/grove-blogs-backup
    permissions: "0644"
    content: |
      30 3 * * * root /usr/local/bin/grove-blogs-backup.sh >> /var/log/grove-blogs-backup.log 2>&1

runcmd:
  # -- Mount the persistent data volume (attached by TF as first data disk)
  - mkdir -p /mnt/blogs-data
  - |
    DEV=$(ls /dev/disk/by-id/scsi-0DO_Volume_* | head -1)
    if ! blkid "$DEV" >/dev/null 2>&1; then mkfs.ext4 "$DEV"; fi
    grep -q "$DEV /mnt/blogs-data" /etc/fstab || echo "$DEV /mnt/blogs-data ext4 defaults,nofail,discard 0 2" >> /etc/fstab
    mount -a
  - mkdir -p /mnt/blogs-data/mysql /mnt/blogs-data/mysql-init /mnt/blogs-data/ghost-hub /mnt/blogs-data/ghost-goldberry /mnt/blogs-data/ghost-ggg /mnt/blogs-data/ghost-nursery

  # -- Docker install (same approach as qa-l3 droplets)
  - curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  - sh /tmp/get-docker.sh
  - systemctl enable docker
  - systemctl start docker

  # -- Randomize secrets (only if placeholders still present - keeps this
  # idempotent across reboots; the data volume outlives the droplet, so a
  # REPLACEMENT droplet must reuse the existing MySQL passwords - operator
  # restores /etc/grove-blogs/.env from the previous droplet or resets
  # MySQL users. First boot on a fresh volume takes the random path.)
  - |
    if grep -q "__MYSQL_ROOT_PASSWORD__" /etc/grove-blogs/.env; then
      for K in MYSQL_ROOT_PASSWORD MYSQL_GHOST_HUB_PASSWORD MYSQL_GHOST_GOLDBERRY_PASSWORD MYSQL_GHOST_GGG_PASSWORD MYSQL_GHOST_NURSERY_PASSWORD; do
        V=$(openssl rand -hex 24)
        sed -i "s|__$${K}__|$V|" /etc/grove-blogs/.env
      done
    fi

  # -- Materialize mysql init.sql with the randomized passwords (first boot
  # on a fresh volume only; mysql entrypoint ignores it once data exists)
  - |
    . /etc/grove-blogs/.env
    sed -e "s|__MYSQL_GHOST_HUB_PASSWORD__|$MYSQL_GHOST_HUB_PASSWORD|" \
        -e "s|__MYSQL_GHOST_GOLDBERRY_PASSWORD__|$MYSQL_GHOST_GOLDBERRY_PASSWORD|" \
        -e "s|__MYSQL_GHOST_GGG_PASSWORD__|$MYSQL_GHOST_GGG_PASSWORD|" \
        -e "s|__MYSQL_GHOST_NURSERY_PASSWORD__|$MYSQL_GHOST_NURSERY_PASSWORD|" \
        /etc/grove-blogs/mysql-init.sql.tpl > /mnt/blogs-data/mysql-init/init.sql
    chmod 0600 /mnt/blogs-data/mysql-init/init.sql

  # -- Bring up the stack
  - docker compose -f /etc/grove-blogs/docker-compose.yml --env-file /etc/grove-blogs/.env -p grove-blogs up -d > /var/log/grove-blogs-up.log 2>&1

  # -- Ready sentinel: all four Ghosts answering on their internal ports
  - |
    for i in $(seq 1 60); do
      OK=0
      for SVC in ghost-hub ghost-goldberry ghost-ggg ghost-nursery; do
        code=$(docker exec grove-blogs-caddy-1 wget -qO- --timeout=5 "http://$SVC:2368/ghost/api/admin/site/" >/dev/null 2>&1 && echo 200 || echo 000)
        [ "$code" = "200" ] && OK=$((OK+1))
      done
      if [ "$OK" = "4" ]; then
        touch /var/lib/cloud/instance/grove-blogs-ready
        echo "[ok] grove-blogs-ready sentinel touched" >> /var/log/grove-blogs-up.log
        exit 0
      fi
      sleep 5
    done
    echo "::error::not all Ghost instances came online" >> /var/log/grove-blogs-up.log
    exit 1
