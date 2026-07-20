#cloud-config
# Grove obs droplet bootstrap: install Docker, write the standalone OpenObserve +
# Keep stack, bring it up. Alert routing + monitors/dashboards are applied
# SEPARATELY by scripts/setup-monitoring.py run against this droplet's public
# URLs (OPENOBSERVE_BASE_URL / KEEP_BASE_URL) from CI or an operator, not here.
# NOTE: keep this template ASCII-only. cloud-init's YAML parser rejects some
# non-ASCII bytes (an em-dash here once broke parsing -> empty cloud config ->
# a bare droplet with nothing installed). GOL-270.
package_update: true

# unzip is needed to unpack the digest-pinned discord-bridge source archive
# (below). Installed at boot before runcmd. Harmless when the discord overlay is
# disabled.
packages:
  - unzip

write_files:
  - path: /etc/grove-obs/.env
    permissions: "0600"
    content: |
      OPENOBSERVE_TAG=${openobserve_tag}
      KEEP_TAG=${keep_tag}
      OPENOBSERVE_ROOT_EMAIL=${openobserve_root_email}
      OPENOBSERVE_ROOT_PASSWORD=${openobserve_root_password}
      ZO_S3_SERVER_URL=${spaces_endpoint}
      ZO_S3_REGION_NAME=us-east-1
      ZO_S3_BUCKET_NAME=${spaces_bucket}
      ZO_S3_ACCESS_KEY=${spaces_access_key}
      ZO_S3_SECRET_KEY=${spaces_secret_key}
      KEEP_WEBHOOK_TOKEN=${keep_webhook_token}
      KEEP_NEXTAUTH_SECRET=${keep_nextauth_secret}
      # env label (qa|prod) stamped on metrics by setup-monitoring.py: ${cost_env}

  - path: /etc/grove-obs/docker-compose.obs.yml
    encoding: b64
    permissions: "0644"
    content: ${compose_obs_b64}

  # Public RUM ingest vhost (GOL-311): Caddyfile + Cloudflare Origin Certificate.
  # b64-encoded so cloud-init's YAML parser never sees Caddyfile braces / PEM
  # bodies (same technique as the compose above). The private key is 0600.
  - path: /etc/grove-obs/caddy/Caddyfile
    encoding: b64
    permissions: "0644"
    content: ${caddyfile_rum_b64}

  - path: /etc/grove-obs/caddy/certs/rum.crt
    encoding: b64
    permissions: "0644"
    content: ${cf_origin_cert_b64}

  - path: /etc/grove-obs/caddy/certs/rum.key
    encoding: b64
    permissions: "0600"
    content: ${cf_origin_key_b64}

%{ if discord_bridge_enabled ~}
  # -- Discord bridge interactions endpoint overlay (GOL-593 / GOL-598) --------
  # This template must stay ASCII-only (see header): a non-ASCII byte here breaks
  # cloud-init's YAML parser -> empty cloud config -> bare droplet (GOL-270).
  # Codifies the hot-applied go-live: the zero-dep Ed25519-verified interactions
  # server + a Cloudflare Tunnel connector that exposes it at
  # https://discord.gatheringatthegrove.com/interactions with NO inbound port and
  # NO origin cert (Discord -> CF edge -> tunnel -> discord-bridge:8787). The
  # overlay attaches to the existing grove-obs_obs network, so it is brought up
  # AFTER the base obs stack in runcmd. Secrets flow from 1P via the tfvars into
  # 0600 env files; the source is delivered as a digest-neutral zip and unpacked
  # to the RO bind-mount path.
  - path: /etc/grove-obs/docker-compose.discord.yml
    encoding: b64
    permissions: "0644"
    content: ${compose_discord_b64}

  - path: /etc/grove-obs/discord-bridge.env
    permissions: "0600"
    content: |
      BUFFER_API_TOKEN=${discord_buffer_api_token}
      DISCORD_BOT_TOKEN=${discord_bot_token}
      DISCORD_APP_ID=${discord_app_id}
      DISCORD_PUBLIC_KEY=${discord_public_key}
      DISCORD_WEEKLY_INSIGHTS_CHANNEL_ID=${discord_insights_channel_id}
      PORT=${discord_bridge_port}

  - path: /etc/grove-obs/cloudflared.env
    permissions: "0600"
    content: |
      TUNNEL_TOKEN=${discord_tunnel_token}

  # apps/discord-bridge source, vendored under compose/discord-bridge-src/ and
  # zipped by data.archive_file. Unpacked to /etc/grove-obs/discord-bridge-src in
  # runcmd, then bind-mounted RO into the node container.
  - path: /etc/grove-obs/discord-bridge-src.zip
    encoding: b64
    permissions: "0644"
    content: ${discord_bridge_src_zip_b64}
%{ endif ~}

runcmd:
  - curl -fsSL https://get.docker.com | sh
  - systemctl enable --now docker
  - cd /etc/grove-obs && docker compose --env-file .env -f docker-compose.obs.yml up -d
%{ if discord_bridge_enabled ~}
  # Bring the discord overlay up AFTER the base stack so the external
  # grove-obs_obs network already exists. Idempotent: unzip -o overwrites, and
  # `compose up -d` converges. The overlay's env_file directives inject secrets.
  - rm -rf /etc/grove-obs/discord-bridge-src && mkdir -p /etc/grove-obs/discord-bridge-src
  - unzip -o /etc/grove-obs/discord-bridge-src.zip -d /etc/grove-obs/discord-bridge-src
  - cd /etc/grove-obs && docker compose -f docker-compose.discord.yml up -d
%{ endif ~}
  - touch /var/lib/cloud/instance/grove-obs-ready
