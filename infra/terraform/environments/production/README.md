# environments/production

ADR-007 Phase 6 production environment. Track 1 (blogs droplet) is live.
Track 2 (Managed PG + Odoo droplet + four App Platform apps) is **also LIVE** as
of late July 2026 — it was stood up via `-target`'d applies and now exists in
prod state (serial 23). The "apply gated on GOL-105" language that used to be
here is dead: GOL-105 was cancelled 2026-07-08 and the tier launched anyway.
GOL-391 tracks the now-inverted concern — blast-radius / drift protection for the
LIVE tier. Its mechanism is **decided (Option A, CEO-accepted 2026-08-07)**:
`prevent_destroy` on the irreplaceable data objects (PG cluster + the `odoo` ERP
database, both volumes, both backups buckets, the reserved IPs), plus the
`prod-plan-guard` CI check (`.github/workflows/prod-plan-guard.yml`) which fails
any PR whose prod plan would **destroy or replace any live resource**. The gate
is now code, not this README sentence.

## ⚠️ Do not run a bare `terraform apply` here (GOL-385)

A full-environment `apply` against current `main` is **not** a routine operation.
The plan this environment was scaffolded against (clean `main`, prod state serial
6) was `15 to add, 5 to change, 2 to destroy`. Prod has since advanced to serial
23 and Track 2 was applied, so the shape has changed — but bare apply is still a
stop-and-escalate for the inverse reason:

- `digitalocean_droplet.blogs must be replaced` — this droplet is **live**, serving
  all four brand blogs. Blog *content* survives (`digitalocean_volume.blogs_data`
  carries `prevent_destroy`), but a replace is a public outage, and until GOL-382
  lands a reserved IP the droplet comes back on a **new IP** that DNS must chase.
- **All of Track 2 is now LIVE** — Managed PG (the ERP database), the Odoo droplet
  + filestore, the Odoo DNS record + firewalls, and the four App Platform apps.
  A bare apply against `main` no longer *launches* them; the danger is the
  opposite — drift-driven **destroy/replace** of live revenue infra. `count`-gating
  them off (the original GOL-391 plan) would itself propose destroying the tier,
  so it is not the fix. The backstop (GOL-391 Option A) is two-layered:
  `prevent_destroy` on the irreplaceable data objects (PG cluster + the `odoo`
  ERP database, both volumes, both backups buckets, reserved IPs), and the
  `prod-plan-guard` CI check which fails any PR whose plan would destroy/replace
  a live resource.

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
- **Reserved IP** for the blogs droplet (GOL-387) - the blog.* records point HERE,
  not at the droplet's ephemeral address, so a replace needs no DNS change. Same
  prerequisite #242's day-2 model assumed, on the one droplet that is already live.
- Cloudflare Origin CA certs (15y, per zone) for proxied TLS
- grove-blogs-backups Spaces bucket + lifecycle
- Apex A records for gatheringatthegrove.com + goldberrygrove.farm
  (imported from Cloudflare during migration; see apex-records.tf) (file arrives with the Task 6 migration)
  **When they land, point them at `digitalocean_reserved_ip.blogs`, not at the droplet.**

### ⚠ The blogs droplet has a PENDING REPLACE - do not run a bare `terraform apply`

A plan against real prod state shows `digitalocean_droplet.blogs must be replaced`, on a
droplet that is **live and serving all four brand blogs**. Two ForceNew attributes drive
it (`replace_paths: [["monitoring"], ["user_data"]]`):

- `user_data` drifted from what was applied. The droplet was created 2026-07-07; this env
  first landed in git 2026-07-12 (#207), so the live box was applied from a working tree
  that was never committed as-is. State stores `user_data` as a SHA1, so what is actually
  on the box **cannot be recovered from Terraform**.
- `monitoring` `false -> true` from GOL-381 (#256). Its four droplet resource alerts
  (cpu/memory/disk/load5) were applied but the flag was not, so the `do-agent` is absent
  and those alerts have no metric source - they report green forever. The uptime/ssl
  alerts are external probes and are unaffected, so a hard outage still pages; what is
  invisible is the slow burn (disk/memory/load). The replace is what makes that half real.

A bare apply here still rebuilds the live blogs as a side effect of whatever else you
were applying. It no longer **rewrites their DNS**: the reserved IP (`159.89.243.121`)
was applied 2026-07-15 and the `blog.*` records point at it, so a replace re-assigns the
same address instead of moving DNS. The replace itself is still an unscheduled outage on
a box whose boot is unproven (GOL-385) — take it in a chosen window:
[`docs/RUNBOOK-blogs-reserved-ip-cutover.md`](../../../../docs/RUNBOOK-blogs-reserved-ip-cutover.md).

## What lives here (Track 2 - APPLIED / LIVE)

> ⚠️ **Status update (GOL-391, 2026-08-06): Track 2 is APPLIED and LIVE in
> prod.** The "apply gated" framing below is historical. The Managed PG cluster,
> the Odoo droplet + filestore, the Odoo DNS record + firewalls, and all four App
> Platform apps exist in prod state (serial 23; `terraform state list` confirms
> them). They were stood up via `-target`'d applies after this environment was
> scaffolded. **The live current risk is the inverse of the original one: a bare
> apply now proposes to DESTROY/REPLACE this tier, not launch it** — so `count`
> gating it off (GOL-391 option 1) is no longer free and is NOT the fix. The
> blast-radius isolation mechanism is under GOL-391 board review (CEO + Founding
> Engineer). Do not re-gate or destroy these resources without that decision.

- Managed Postgres cluster (basic tier, db-s-1vcpu-2gb, daily backups + 7d
  PITR, private VPC) + Odoo DB/user - see postgres.tf
- Odoo droplet (s-2vcpu-4gb) + Caddy (Origin CA cert files) + durable filestore
  block volume (GOL-93) + Managed PG trusted-sources firewall - see odoo.tf
- **Reserved IP** for the Odoo droplet (GOL-382) - the A record points HERE, not
  at the droplet's ephemeral address, so an immutable droplet replace needs no
  DNS change. This is the prerequisite #242's day-2 model assumed.
- odoo.gatheringatthegrove.com Cloudflare-proxied A record (the record GOL-93's
  /web/image edge-cache rule was waiting on)
- **Nightly filestore backup** (GOL-99): `grove-odoo-backups` Spaces bucket +
  bucket-scoped key; rclone mirror at 03:00 UTC with a Healthchecks dead-man's
  switch. Restore procedure + rehearsal:
  [`docs/RUNBOOK-odoo-filestore-restore.md`](../../../../docs/RUNBOOK-odoo-filestore-restore.md)

### Before the prod apply (GOL-382)

- Set `odoo_backup_healthchecks_ping_url` in `terraform.tfvars` (create the
  check in Healthchecks first, period 1d / grace 6h). It defaults to `""`, which
  keeps `plan` working but leaves the backup **unmonitored** - and an
  unmonitored backup is not a backup.
- `prevent_destroy` is set on the irreplaceable data objects: both volumes, the
  Managed PG cluster **and the `odoo` ERP database inside it** (GOL-391 — the
  database itself, not just the cluster shell, holds every product/order/customer
  row), both backups buckets, and the reserved IP. A `terraform destroy` will
  fail loudly on them **by design**. To genuinely remove one, delete its
  `lifecycle` block in a reviewed commit first - that deliberate speed bump is
  the feature. The replaceable Track 2 resources (the Odoo droplet, the four App
  Platform apps, DNS, firewalls) intentionally have **no** `prevent_destroy` so
  legitimate rebuilds are not blocked; they are protected instead by the
  `prod-plan-guard` CI check below, which fails any PR whose plan would
  destroy/replace them.
- Reuses Track 1's SSH keys + the hub-zone Origin CA cert (its
  `*.gatheringatthegrove.com` SAN covers `odoo.`)

## What lives here (Track 2 step 3 - GOL-116, App Platform apps LIVE)

- App Platform apps: `grove-hub-prod` + 3 tenants (goldberry/ggg/nursery),
  pro tier (`apps-d-1vcpu-0.5gb`, ~$12/mo each, ADR-007 D6) - see apps.tf
- Env wiring: GROVE_ODOO_URL/ODOO_URL → https://odoo.gatheringatthegrove.com;
  Ghost URLs → the live blog.* hosts; real per-tenant ODOO_API_KEY + shared
  GROVE_REVALIDATE_SECRET + Ghost content keys (all GENERAL scope, injected via
  TF_VAR from 1Password - stubs keep `plan` working pre-launch)
- DO-native DEPLOYMENT_FAILED / DOMAIN_FAILED alerts (alert path #2)
- **No `domain{}` blocks yet.** The four brand apexes are a one-way-door launch
  cutover (GOL-116 decisions #1 CF-proxied-apex TLS pattern + #2 CEO-coordinated
  flip). Until resolved+applied, the apps serve only on their
  `*.ondigitalocean.app` ingress and the apexes keep serving Ghost. See the
  "Apex cutover" block at the bottom of apps.tf.

> ⚠️ **The GOL-105 "apply gate" is DEAD, and Track 2 has since gone LIVE.**
>
> Two layers of staleness, both corrected here (GOL-391):
> 1. GOL-105 (the soak sign-off that was to gate the apply) was cancelled
>    2026-07-08. There is no scheduled soak sign-off.
> 2. Track 2 was **applied anyway** (via `-target`'d applies) and is now LIVE:
>    the prod Managed PG cluster, the Odoo droplet + filestore, and all four App
>    Platform apps exist in prod state (serial 23). The earlier claim in this
>    file that "no prod Odoo droplet and no prod Managed PG exist" was wrong and
>    has been removed — acting on it could have caused real damage.
>
> **Net:** there is nothing left to "gate the launch of" — it launched. The
> historical soak criteria below are retained only for context. The open GOL-391
> question is now blast-radius / drift protection for the LIVE tier (does every
> live Track 2 resource have an appropriate destroy/replace guard, and should
> Track 2 move to its own state), not preventing an accidental launch. That
> mechanism decision is owned by the CEO + Founding Engineer.

**Historical soak criteria (GOL-105 — cancelled; never performed; context
only):** Managed PG
perf + Odoo pool acceptable; App Platform TLS auto-renew clean; GHCR autodeploys
reliable; durable filestore + droplet-replace test validated (GOL-93); three
alert paths green; no unresolved incidents across the window.

Still pending (GOL-116): resolve apex-cutover decisions #1/#2, add the
`domain{}` blocks + Cloudflare apex→ingress records, and coordinate the
launch-day flip with the CEO.

## History

The pre-Level-3 monolith production config that previously lived here
(never applied; "DO NOT DEPLOY YET") was replaced 2026-07 by this
Phase 6 shape per the Grove Production Launch spec.
