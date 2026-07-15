# environments/production

ADR-007 Phase 6 production environment. Track 1 (blogs droplet) is live.
Track 2 (Managed PG + Odoo droplet, this scaffold) is authored but its **apply
is gated** on the QA L3 soak sign-off (~2026-07-21+) and the @CEO final go
(GOL-105). App Platform (the fourth piece) is a GOL-105 child issue.

## ⚠️ Do not run a bare `terraform apply` here (GOL-385)

A full-environment `apply` against current `main` is **not** a routine operation.
The last verified plan (clean `main`, prod state serial 6) was
`15 to add, 5 to change, 2 to destroy`, and it included:

- `digitalocean_droplet.blogs must be replaced` — this droplet is **live**, serving
  all four brand blogs. Blog *content* survives (`digitalocean_volume.blogs_data`
  carries `prevent_destroy`), but a replace is a public outage, and until GOL-382
  lands a reserved IP the droplet comes back on a **new IP** that DNS must chase.
- **All of Track 2** — Managed PG, the Odoo droplet, and the four App Platform
  apps — which are supposed to be gated on the GOL-105 soak sign-off. That gate is
  **prose in this README, not a constraint in code**: nothing stops an apply from
  standing the whole tier up.

Until GOL-385 closes, changes here are applied `-target`'d to the specific
resources being changed, and any plan that proposes replacing
`digitalocean_droplet.blogs` is a **stop-and-escalate**, not a thing to approve.

## Apply (manual by decision, `-target`'d — see the warning above)

1. `cp backend.hcl.example backend.hcl`
2. `.env.op` (committed in this dir) maps op:// refs -> TF_VAR_* + AWS_*. Required:
   - TF_VAR_do_token, TF_VAR_cloudflare_api_token (ACCOUNT-scoped token incl. "SSL and Certificates: Edit" - authorizes Origin CA cert issuance; the legacy Origin CA Key is deprecated)
   - TF_VAR_spaces_access_id, TF_VAR_spaces_secret_key
   - AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY (state backend)
   - **TF_VAR_grove_revalidate_secret** — required, no default, `>=32` chars.
     `plan` hard-fails without it, so it is not optional even for a read-only
     plan. The no-default is deliberate (a bare apply must not be able to ship a
     placeholder secret to prod); it is currently **commented out** in `.env.op`
     pending the 1P field, which is why the documented steps below do not yet run
     end-to-end from a clean checkout.
   - `healthchecks_ping_url` — feeds the blogs droplet **user_data**; see
     "Reproducibility" below before setting it.
   - `admin_ip_cidr`, `region`, `blogs_droplet_size` now have codified defaults
     matching live prod. Do **not** re-supply them from a local tfvars.
3. `terraform init -backend-config=backend.hcl`
4. `op run --env-file=.env.op -- terraform plan`
5. `op run --env-file=.env.op -- terraform apply -target=...`

## Reproducibility (GOL-385, open)

`grove-prod-blogs` (id `582968733`) was created **2026-07-07T19:49:15Z**. This
repo's git history **begins at the root commit `73603ed`, 2026-07-12** — an import
snapshot. The exact template bytes that built the live droplet therefore exist at
**no commit in this repository**, and no `terraform.tfvars` can change that.

The provider stores `user_data` as a SHA1. State holds
`f6071c899edb6edfac10116029348f0715887c56`; today's templates render
`0baae2b4…` with `healthchecks_ping_url = ""` and `33bdef3c…` with the old
`REPLACE-UUID` placeholder. Neither reproduces state, which is why every plan
proposes a replace.

**Consequence:** prod is not currently reproducible from code, and the only
inputs that can still be recovered have been — `admin_ip_cidr`
(`74.47.41.38/32`, read back out of the live firewall's port-22 rule), `region`,
and `blogs_droplet_size` are now codified defaults. The single genuinely
unrecoverable input is `healthchecks_ping_url`, which exists only inside the
hashed `user_data`.

## What lives here (Track 1)

- Blogs droplet (4x Ghost 6 + MySQL 8 + Caddy) - see blogs.tf
- blog.{zone} DNS records in all four Cloudflare brand zones
- Cloudflare Origin CA certs (15y, per zone) for proxied TLS
- grove-blogs-backups Spaces bucket + lifecycle
- Apex A records for gatheringatthegrove.com + goldberrygrove.farm
  (imported from Cloudflare during migration; see apex-records.tf) (file arrives with the Task 6 migration)

## What lives here (Track 2 - GOL-105, apply gated)

- Managed Postgres cluster (basic tier, db-s-1vcpu-2gb, daily backups + 7d
  PITR, private VPC) + Odoo DB/user - see postgres.tf
- Odoo droplet (s-2vcpu-4gb) + Caddy (Origin CA cert files) + durable filestore
  block volume (GOL-93) + Managed PG trusted-sources firewall - see odoo.tf
- odoo.gatheringatthegrove.com Cloudflare-proxied A record (the record GOL-93's
  /web/image edge-cache rule was waiting on)
- Reuses Track 1's SSH keys + the hub-zone Origin CA cert (its
  `*.gatheringatthegrove.com` SAN covers `odoo.`)

## What lives here (Track 2 step 3 - GOL-116, apply gated)

- App Platform apps: `grove-hub-prod` + 3 tenants (goldberry/ggg/nursery),
  pro tier (`apps-d-1vcpu-0.5gb`, ~$12/mo each, ADR-007 D6) - see apps.tf
- Env wiring: GROVE_ODOO_URL/ODOO_URL → https://odoo.gatheringatthegrove.com;
  Ghost URLs → the live blog.* hosts; real per-tenant ODOO_API_KEY + shared
  GROVE_REVALIDATE_SECRET + Ghost content keys (all GENERAL scope, injected via
  TF_VAR from 1P/Infisical - stubs keep `plan` working pre-launch)
- DO-native DEPLOYMENT_FAILED / DOMAIN_FAILED alerts (alert path #2)
- **No `domain{}` blocks yet.** The four brand apexes are a one-way-door launch
  cutover (GOL-116 decisions #1 CF-proxied-apex TLS pattern + #2 CEO-coordinated
  flip). Until resolved+applied, the apps serve only on their
  `*.ondigitalocean.app` ingress and the apexes keep serving Ghost. See the
  "Apex cutover" block at the bottom of apps.tf.

**Soak sign-off (GOL-105) must be green before `terraform apply`:** Managed PG
perf + Odoo pool acceptable; App Platform TLS auto-renew clean; GHCR autodeploys
reliable; durable filestore + droplet-replace test validated (GOL-93); three
alert paths green; no unresolved incidents across the window. Then @-mention the
CEO here for the final go.

Still pending (GOL-116): resolve apex-cutover decisions #1/#2, add the
`domain{}` blocks + Cloudflare apex→ingress records, and coordinate the
launch-day flip with the CEO.

## History

The pre-Level-3 monolith production config that previously lived here
(never applied; "DO NOT DEPLOY YET") was replaced 2026-07 by this
Phase 6 shape per the Grove Production Launch spec.
