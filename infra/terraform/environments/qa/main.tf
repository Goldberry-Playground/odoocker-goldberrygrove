###############################################################################
# Grove QA — long-lived(ish) QA environment.
#
# Per Josh's dev cycle: local → orbstack → validate → push to `qa` branch →
# auto-deploy to this env → real humans test → push to main → prod + qa teardown.
#
# Lifecycle:
#   `make qa-apply`    — provisions OR updates the QA env in place
#   `make qa-destroy`  — tears down (called by qa-teardown.yml on prod-deploy success)
#
# Cost while running: ~$24/mo (s-2vcpu-4gb in nyc3) + ~$0/mo for Cloudflare/DO DNS.
#
# What this manages (self-contained — no bootstrap-env prerequisite):
#   1. Cloudflare → DO NS delegation for qa.gatheringatthegrove.com
#   2. DO domain for qa.gatheringatthegrove.com + child A/CNAME records
#   3. DO SSH key (operator-generated; public key uploaded)
#   4. DO droplet (Ubuntu 24.04, s-2vcpu-4gb)
#   5. DO firewall (SSH from admin_ip_cidr, 80/443 from anywhere)
#   6. cloud-init: docker install + compose stack bring-up + /grove-ready sentinel
#
# What this does NOT manage:
#   - Ghost containers (frontends point at LIVE blog.goldberrygrove.farm via
#     ghost_key_goldberry, or gracefully degrade when empty)
#   - Persistent volumes (QA is ephemeral — testers create data, it dies
#     on the next qa-destroy + qa-apply cycle. By design. The brief PR #81
#     experiment with a persistent DO volume for Caddy /data was reverted
#     in PR #82 in favor of DNS-01 wildcard TLS, which eliminates the LE
#     rate-limit class that originally motivated the volume.)
#   - Snapshot restore from preview-data Spaces bucket (preview env does
#     that; QA starts empty)
###############################################################################

provider "digitalocean" {
  token = var.do_token
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}

locals {
  qa_zone = "${var.qa_subdomain}.${var.cloudflare_zone_name}"
  tags = [
    "env-qa",
    "project-grove",
  ]
}

data "cloudflare_zone" "apex" {
  name = var.cloudflare_zone_name
}

# ── Cloudflare → DigitalOcean NS delegation for qa.<apex> ───────────────────
# Three NS records under the apex zone in Cloudflare point at DO's nameservers,
# delegating the qa subdomain's DNS management to DO. Same pattern as the
# preview env's bootstrap (PR #19 in odoocker).

resource "cloudflare_record" "qa_ns" {
  for_each = toset([
    "ns1.digitalocean.com",
    "ns2.digitalocean.com",
    "ns3.digitalocean.com",
  ])

  zone_id = data.cloudflare_zone.apex.id
  name    = var.qa_subdomain
  type    = "NS"
  value   = each.value
  ttl     = 1 # 1 = Cloudflare "Auto"
}

# ── DO domain (the delegated zone DO now manages) ───────────────────────────

resource "digitalocean_domain" "qa" {
  name = local.qa_zone
  # No ip_address here — child records (below) handle individual hosts.
}

# ── SSH keys ────────────────────────────────────────────────────────────────
#
# Two keys are registered with the droplet, both LONG-LIVED:
#
#   1. qa_deploy  — CI key. Public key hardcoded in var.ci_ssh_public_key.
#      Private key in 1Password (grove_qa_ci_ssh_private_key) and Infisical
#      (GROVE_QA_CI_SSH_PRIVATE_KEY); qa-deploy.yml fetches the private half
#      via OIDC each run and writes it to ~/.ssh/grove-qa-deploy on the
#      runner. Used by the workflow's grove-ready sentinel poll.
#
#      WHY LONG-LIVED: an earlier design generated this key fresh per run.
#      That meant every workflow run's TF plan included a REPLACE on this
#      resource (because the public_key string differed), and the deploy-
#      scoped DO token lacks `ssh_key:delete`. Result: every redeploy after
#      the first hit 403 and needed a manual web-UI delete of the orphan
#      key. Switching to a long-lived key eliminates that friction
#      permanently.
#
#      ONE-TIME TRANSITION: existing TF state references this resource with
#      the OLD random public_key. After applying this change, TF would plan
#      a REPLACE (destroy old in DO + create new in DO) which still hits
#      the 403. The transition is therefore:
#        1. Delete the orphan in DO web UI (or via teardown token if scoped)
#        2. terraform state rm digitalocean_ssh_key.qa_deploy
#        3. terraform apply (plans CREATE only, no destroy)
#
#   2. qa_admin   — Human admin key. Public key hardcoded in
#      var.admin_ssh_public_key (default = Josh's grove-qa-admin pubkey).
#      Private key on Josh's laptop at ~/.ssh/grove-qa-admin AND backed up
#      in 1Password (grove_qa_admin_ssh_private_key) for recovery. Used by
#      humans to SSH in for debugging.
#
# See README.md > "SSH access" for usage.

resource "digitalocean_ssh_key" "qa_deploy" {
  name       = "grove-qa-deploy"
  public_key = var.ci_ssh_public_key
}

# qa_admin is managed OUT OF BAND by Josh (created once at his discretion,
# rotated when he chooses). TF references it via a data source so TF will
# never attempt to create/destroy/replace it. If the key is missing in DO,
# `terraform plan` errors -- which is the correct behavior (we want a hard
# stop, not silent recreation of an admin credential).
#
# Per DO TF tutorial recommendation: "Retrieve the key using a data source"
# instead of `resource` when the key's lifecycle isn't owned by this TF env.
data "digitalocean_ssh_key" "qa_admin" {
  name = "grove-qa-admin"
}

# ── Persistent Caddy /data volume ───────────────────────────────────────────
#
# Caddy stores its LE account key + issued certs in /data. By default that
# lives in a Docker named volume on the droplet's root fs, which dies with
# every droplet recreate -- which makes every redeploy a fresh ACME request
# and burns 1 of LE's 5/week-per-identifier budget. We blew through that
# budget on 2026-06-26 with both HTTP-01 (5 separate identifiers) AND with
# DNS-01 wildcard (apex+wildcard combined identifier set).
#
# This volume persists Caddy's /data across droplet recreates. After the
# FIRST successful ACME issuance, the cert lives ~80 days; subsequent
# droplet recreates re-use it. Cert request frequency drops 10x.
#
# Differs from the reverted PR #81 in:
#   - tags = local.tags (discoverable via tag-based scripts)
#   - region-prefixed name (no collision if QA copy-spun in another region)
#   - LABEL=data filesystem (mount by label, not by-id path)
#   - NO prevent_destroy (it doesn't actually protect since script teardown
#     bypasses TF; operator can destroy via `terraform destroy -target=...`)
#   - cloud-init uses native `mounts:` module (declarative, handles
#     device-wait + fstab idempotently), not the runcmd dance from PR #81
#     that the code review caught 4 race-condition bugs in
#
# Lifecycle: created on first qa-apply; persists across droplet
# teardown/recreate via the standard volume_attachment dance. The
# qa-teardown-droplet.sh script pre-detaches the volume before destroying
# the droplet (added in this PR), so the next apply can reattach cleanly
# without hitting the DO-side "volume still attached to gone droplet"
# race window.
resource "digitalocean_volume" "caddy_data" {
  region                   = var.region
  name                     = "${var.region}-grove-qa-caddy-data"
  size                     = 1
  initial_filesystem_type  = "ext4"
  initial_filesystem_label = "data"
  tags                     = local.tags
  description              = "Persistent Caddy /data (LE certs + ACME account). Survives droplet teardown to avoid LE rate limits on iterative QA cycles. ~$0.10/mo."
}

resource "digitalocean_volume_attachment" "caddy_data" {
  droplet_id = digitalocean_droplet.qa.id
  volume_id  = digitalocean_volume.caddy_data.id
}

# ── Droplet ─────────────────────────────────────────────────────────────────

resource "digitalocean_droplet" "qa" {
  name   = "grove-qa"
  size   = var.droplet_size
  image  = var.droplet_image
  region = var.region
  tags   = local.tags

  ssh_keys = [
    digitalocean_ssh_key.qa_deploy.fingerprint,     # CI key (TF-managed; long-lived per PR #63)
    data.digitalocean_ssh_key.qa_admin.fingerprint, # admin key (out-of-band; TF references only)
  ]

  # Self-bootstrapping via cloud-init. See cloud-init.yaml.tpl for the
  # script — it installs docker, writes /etc/grove/{.env,docker-compose.yml,
  # Caddyfile}, brings the stack up, and touches /var/lib/cloud/instance/
  # grove-ready when the stack is responding on https://localhost/.
  user_data = templatefile("${path.module}/cloud-init.yaml.tpl", {
    qa_zone             = local.qa_zone
    odoo_image_tag      = var.odoo_image_tag
    frontend_image_tags = var.frontend_image_tags
    caddy_image_tag     = var.caddy_image_tag
    ghost_key_goldberry = var.ghost_key_goldberry
    # Deployer-generated (see variables.tf) -- flows into /etc/grove/.env so
    # the workflow can construct QA_PORTAL_DATABASE_URL without SSH.
    qa_portal_pg_password = var.qa_portal_pg_password
    # DO API token for Caddy's DNS-01 ACME challenge. Same token TF uses
    # (domain:write is the scope needed to manage _acme-challenge TXT
    # records under the delegated qa zone). Flows to /etc/grove/.env and
    # then into the caddy container's DO_API_TOKEN env var.
    do_token_for_caddy = var.do_token
    # ACME endpoint for Caddy (prod or staging). Default = prod (real
    # browser-trusted certs); operator opts into staging via qa-deploy.yml's
    # use_staging_acme workflow_dispatch input when iterating heavily.
    # Flows to /etc/grove/.env -> caddy container's ACME_CA env -> Caddyfile.
    acme_endpoint = var.acme_endpoint
    # base64-encode the Caddyfile + compose YAML so cloud-init's YAML parser
    # never sees their content -- bypasses the whole class of "embedded
    # block-scalar broke YAML parse" failures we hit on 2026-06-24 (PRs #62
    # for Unicode, #65 for $$ substitution, #66 for indent). Per DO cloud-config
    # tutorial: use `encoding: b64` for write_files content with untrusted
    # or complex formatting.
    compose_yml_b64   = base64encode(file("${path.module}/compose/docker-compose.qa.yml"))
    caddyfile_tpl_b64 = base64encode(replace(file("${path.module}/compose/Caddyfile.tpl"), "$${QA_ZONE}", local.qa_zone))
    # Ghost autoseed pair (task 97d) -- shipped from the repo's scripts/ dir
    # so there's one source of truth; cloud-init writes them to /opt/grove/.
    ghost_bootstrap_js_b64 = filebase64("${path.module}/../../../../scripts/ghost-bootstrap.js")
    ghost_autoseed_sh_b64  = filebase64("${path.module}/../../../../scripts/qa-ghost-autoseed.sh")
  })

  monitoring = false

  # DO API droplet delete can hang past the provider's default 60s context
  # deadline (observed in qa-deploy run 28134739576 — destroy + create both
  # tripped the deadline). Bumping both create and delete to 15m absorbs the
  # transient DO API slowness without making the workflow appear stuck.
  timeouts {
    create = "15m"
    delete = "15m"
  }
}

# ── DNS records inside the delegated qa zone ────────────────────────────────

# Apex A record — qa.gatheringatthegrove.com → droplet IP. The hub frontend
# is served here (no subdomain prefix).
resource "digitalocean_record" "qa_apex" {
  domain = digitalocean_domain.qa.name
  type   = "A"
  name   = "@" # the apex of the delegated qa zone
  value  = digitalocean_droplet.qa.ipv4_address
  ttl    = 300
}

# Per-tenant CNAMEs — qa-goldberry.qa.gatheringatthegrove.com, etc.
# Caddy on the droplet routes by Host header to the right frontend container.
#
# Naming choice: we use child labels (goldberry → goldberry.qa.gatheringatthegrove.com)
# instead of a flat `qa-goldberry.gatheringatthegrove.com` prefix, because the
# delegation gives DO control over the entire qa subdomain. Cleaner.
resource "digitalocean_record" "tenant" {
  for_each = toset(var.tenant_subdomains)

  domain = digitalocean_domain.qa.name
  type   = "CNAME"
  name   = each.key
  value  = "@" # FQDN within the qa zone — resolves to qa.gatheringatthegrove.com
  ttl    = 300
}

# ── Firewall ────────────────────────────────────────────────────────────────

resource "digitalocean_firewall" "qa" {
  name        = "grove-qa-fw"
  droplet_ids = [digitalocean_droplet.qa.id]

  # SSH — scoped to the operator
  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = [var.admin_ip_cidr]
  }

  # HTTP — Caddy redirects 80 → 443 here. TLS is issued via DNS-01 (DO API),
  # so port 80 is NOT load-bearing for cert issuance -- it's open only for
  # browsers that hit http:// out of habit. Safe to close in a hardening pass.
  inbound_rule {
    protocol         = "tcp"
    port_range       = "80"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  # HTTPS — public surface
  inbound_rule {
    protocol         = "tcp"
    port_range       = "443"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "icmp"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}
