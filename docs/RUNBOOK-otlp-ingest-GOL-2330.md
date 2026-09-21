# Runbook — public OTLP ingest endpoint (GOL-2330)

`https://otlp-ingest.gatheringatthegrove.com/api/default/v1/{traces,metrics,logs}`
lets off-droplet OTLP shippers (tier-2 GitHub-Actions Playwright journeys) send
to OpenObserve on grove-obs. Parent EPIC: GOL-2323.

Browser RUM does **not** use this host: a Bearer shipped in page JS is public.
RUM keeps using `rum.gatheringatthegrove.com/rum/*` (GOL-311, client token).

## Request path

```
shipper --Authorization: Bearer <T>--> Cloudflare edge (hub zone, Full(strict))
   WAF custom rule (cloudflare-policy, inside geo_block): host == otlp-ingest
   and Authorization != "Bearer <T>"  ->  403 at the edge, never reaches origin
 -> grove-obs :443 (DO firewall: Cloudflare IPs only)
   Caddy vhost (observability/compose/Caddyfile-rum.tpl):
     POST + /api/<org>/v1/(traces|metrics|logs) + Bearer <T> re-checked
     (CF IPs are shared by every CF customer, so the origin can't trust the edge)
     Authorization replaced with OpenObserve Basic <ingest credential>
     anything else -> 403 "grove-obs: forbidden"
 -> openobserve:5080
```

## Everything ships inert

All of it is committed but **renders nothing** until the inputs exist, so merging
is a no-op plan in both envs:

| Env | Input (default empty) | Effect while empty |
|---|---|---|
| observability | `otlp_ingest_bearer_token`, `otlp_upstream_credentials`, `otlp_origin_cert_pem`, `otlp_origin_key_pem` | vhost + cert files not rendered; Caddyfile and `user_data` byte-identical to today (verified), so no droplet replace |
| cloudflare-policy | `otlp_ingest_bearer_token` | no WAF rule |
| cloudflare-policy | `otlp_ingest_origin_ip` | no DNS record |

## Activation (Josh, gated: CF + 1Password + obs droplet replace)

1. **Mint into 1Password** `Goldberry Grove - Admin / Grove Infra`:
   - `otlp_ingest_bearer_token` = `openssl rand -hex 32` (charset `[A-Za-z0-9._~-]`, 32+ chars; enforced by validation).
   - `obs_otlp_origin_cert` / `obs_otlp_origin_key` = a Cloudflare Origin
     Certificate for `otlp-ingest.gatheringatthegrove.com` (CF dashboard ->
     SSL/TLS -> Origin Server -> Create). The RUM cert covers `rum.*` only.
   - `obs_otlp_upstream_credentials` = `email:secret` of a **least-privilege
     OpenObserve ingestion credential** (a service account, or the `default`
     org ingestion passcode from OO UI -> Ingestion). Not the root password.
2. **observability apply** with the 4 obs inputs set (tfvars from 1P, same way
   as `cf_origin_cert_pem`). This changes `user_data` => **grove-obs droplet is
   REPLACED** (OpenObserve data lives in Spaces; confirm before applying).
   `terraform plan` must show only `module.obs_droplet` replace.
3. **cloudflare-policy apply** with `TF_VAR_otlp_ingest_bearer_token` (add
   `TF_VAR_otlp_ingest_bearer_token=op://Goldberry Grove - Admin/Grove Infra/otlp_ingest_bearer_token`
   to `.env.op` now that the field exists — `op run` hard-fails on a missing
   field, which is why it isn't there yet) and
   `TF_VAR_otlp_ingest_origin_ip=<obs_droplet_ip output>`. The CF token needs
   `Zone -> DNS -> Edit` on the hub zone. Plan must show: `geo_block` hub
   ruleset update in place (+1 rule) and `cloudflare_record.otlp_ingest[0]`
   create. The ruleset `name` is unchanged, so there's no ForceNew.
4. **Verify** from any off-droplet host:
   `OTLP_INGEST_BEARER=$(op read "op://Goldberry Grove - Admin/Grove Infra/otlp_ingest_bearer_token") infra/terraform/environments/observability/scripts/smoke-otlp-ingest.sh`
   Expect no/wrong Bearer -> 403 **from the edge**, correct Bearer -> 2xx, and
   `grove_otlp_ingest_smoke` visible in OpenObserve.
5. Add GitHub Actions secret `OTLP_INGEST_BEARER` on the repos that run tier-2
   journeys. OTel exporters: `OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp-ingest.gatheringatthegrove.com/api/default`,
   `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer ${OTLP_INGEST_BEARER}`.

## Rotate the Bearer

Re-mint the 1P field, then apply **cloudflare-policy** (edge, in place) and
**observability** (origin; this replaces the droplet), then update the GH secret.
Between the two applies one layer rejects the new token, so rotate in a quiet window.

## Rollback

- Fastest: unset `otlp_ingest_origin_ip` and apply cloudflare-policy. That removes the DNS
  record, so the host stops resolving.
- Unset `otlp_ingest_bearer_token` and apply cloudflare-policy to drop the WAF rule. Don't do
  this while the DNS record exists; the origin still re-checks the Bearer, though.
- Obs side: empty the 4 inputs and apply to go back to the RUM-only Caddyfile (droplet replace).

## Notes

- The token sits in TF state (encrypted Spaces backend), in the CF ruleset expression, and in
  the obs `user_data`. That's inherent to a WAF-expression Bearer check, and the
  obs env already carries other secrets in `user_data` the same way.
- Free-plan zones allow 5 custom rules. The hub zone uses 2 with this rule (geo + OTLP).
