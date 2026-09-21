variable "cloudflare_api_token" {
  description = "Cloudflare API token. Needs Zone -> Zone -> Read AND Zone -> Firewall Services -> Edit on every zone in var.zone_names, PLUS Zone -> Cache Rules -> Edit on gatheringatthegrove.com (for the GOL-93 Odoo /web/image/* cache ruleset), PLUS Zone -> DNS -> Edit on gatheringatthegrove.com once var.otlp_ingest_origin_ip is set (GOL-2330 otlp-ingest record). From GoldberryGrove Infra / cloudflare_api_token."
  type        = string
  sensitive   = true
}

variable "zone_names" {
  description = "Cloudflare zones that get the edge policy rules. Only list zones that actually exist on the account -- the data lookup fails loudly for missing ones (that's the point: a typo'd or not-yet-migrated domain should break the plan, not silently skip protection)."
  type        = set(string)
  default = [
    "atthegrovenursery.com",
    "gatheringatthegrove.com",
    "goldberrygrove.farm",
    "woodworkingeorge.com",
  ]
}

variable "otlp_ingest_host" {
  description = "Cloudflare-proxied hostname for the OpenObserve OTLP ingest endpoint (GOL-2330). The WAF Bearer rule below is scoped to this exact host, so off-droplet OTLP shippers (tier-2 GitHub-Actions Playwright journeys + browser RUM) reach OpenObserve only with a valid Authorization: Bearer <ingest-token>. Must live in the hub zone (gatheringatthegrove.com); its proxied A record is created here once var.otlp_ingest_origin_ip is set -- see docs/RUNBOOK-otlp-ingest-GOL-2330.md. Changing the host only re-scopes the rule expression (no ForceNew)."
  type        = string
  default     = "otlp-ingest.gatheringatthegrove.com"

  validation {
    condition     = endswith(var.otlp_ingest_host, ".gatheringatthegrove.com")
    error_message = "otlp_ingest_host must be a subdomain of gatheringatthegrove.com (the Bearer rule and DNS record are hub-zone-only)."
  }
}

variable "otlp_ingest_bearer_token" {
  description = "Bearer token the CF WAF rule requires on the OTLP ingest host (GOL-2330). Sourced from 1Password (op://Goldberry Grove - Admin/Grove Infra/otlp_ingest_bearer_token) via TF_VAR_otlp_ingest_bearer_token (add the line to .env.op only AFTER the field exists -- op run hard-fails on an unresolvable reference); NEVER hardcode. Empty string (the default) keeps the Bearer rule AUTHORED-BUT-INERT: the dynamic rule renders zero blocks, so a plan against the live zone is a clean no-op until Josh mints + injects the token. Non-empty => the rule blocks every request to var.otlp_ingest_host whose Authorization header does not exactly match `Bearer <this>`. Scoped + rotatable: rotate by re-minting in 1P and re-applying. NOTE: the value lands in TF state (encrypted S3 backend) and is visible in the CF ruleset expression (within Josh's CF trust boundary) -- that is inherent to a WAF-expression Bearer check and is the pattern the issue specifies."
  type        = string
  sensitive   = true
  default     = ""

  # Restricted charset: the value is interpolated into a CF rules-language
  # string literal, so a quote/backslash would break (or rewrite) the expression.
  validation {
    condition     = can(regex("^([A-Za-z0-9._~-]{32,})?$", var.otlp_ingest_bearer_token))
    error_message = "otlp_ingest_bearer_token must be empty or >=32 chars of [A-Za-z0-9._~-] (e.g. `openssl rand -hex 32`)."
  }
}

variable "otlp_ingest_origin_ip" {
  description = "Public IPv4 of the grove-obs droplet (observability env output obs_droplet_ip). Non-empty => creates the PROXIED A record otlp_ingest_host -> this IP (GOL-2330). Empty (default) => no record, so nothing is exposed until the obs-side OTLP vhost is live. Set only AFTER the obs apply renders the vhost, and only together with otlp_ingest_bearer_token (a proxied host without the Bearer rule would be edge-open; the origin still re-checks the Bearer)."
  type        = string
  default     = ""

  validation {
    condition     = var.otlp_ingest_origin_ip == "" || can(cidrhost("${var.otlp_ingest_origin_ip}/32", 0))
    error_message = "otlp_ingest_origin_ip must be empty or an IPv4 address."
  }
}

variable "blocked_countries" {
  description = "ISO 3166-1 alpha-2 country codes whose traffic is blocked at the Cloudflare edge, on every proxied hostname in every zone. Business rationale 2026-07-04: Grove businesses ship nowhere near CN/RU and both are dominant sources of bot/scanner traffic; blocking at the edge cuts noise before it reaches any origin."
  type        = set(string)
  default     = ["CN", "RU"]

  validation {
    condition     = alltrue([for c in var.blocked_countries : can(regex("^[A-Z]{2}$", c))])
    error_message = "blocked_countries entries must be 2-letter uppercase ISO country codes (e.g. CN, RU)."
  }
}
