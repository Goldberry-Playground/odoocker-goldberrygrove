# Runbook — Mailgun SMTP transactional email (GOL-248)

Consolidates **all** transactional email onto Mailgun SMTP, per the CEO-ratified
GOL-245 decision (single vendor: Mailgun for both bulk newsletter *and*
transactional; no SES/Resend).

- **Ghost** (4 prod instances): magic links, member signup/confirmation, welcome
  emails, and Ghost 6 staff-login device-verification codes.
- **Odoo**: storefront order confirmations (was penciled as SES in the launch plan).

Bulk newsletter sending is **out of scope** here — it already runs through Ghost's
one Mailgun *API* config. SMTP is a separate Ghost mail block, so there is no
collision with the newsletter config.

## Sending-domain split (why a separate `mg.<domain>`)

Transactional mail sends from a dedicated Mailgun sending subdomain per brand:

| Tenant    | Brand domain             | Transactional sending subdomain     |
|-----------|--------------------------|-------------------------------------|
| hub       | gatheringatthegrove.com  | `mg.gatheringatthegrove.com`        |
| goldberry | goldberrygrove.farm      | `mg.goldberrygrove.farm`            |
| ggg       | woodworkingeorge.com     | `mg.woodworkingeorge.com`           |
| nursery   | atthegrovenursery.com    | `mg.atthegrovenursery.com`          |

Keeping this stream on `mg.<domain>` isolates it from (a) the bulk-newsletter
reputation and (b) the human Gmail/Workspace mail on the apex, so a bulk
reputation dip cannot break magic links or receipts.

## Prerequisites (blocking — see GOL-248 / GOL-244)

1. **Mailgun private API key in 1Password.** Account exists (`Goldberry Grove -
   Admin` → `Mailgun | Goldberry Grove`, login only). Josh adds the **private API
   key** so Engineering can provision domains + retrieve SMTP creds via the API.
2. **Sending subdomains added in Mailgun** (`POST /v4/domains` per row above).
3. **DNS auth records verified — GOL-244 work, do not duplicate.** For each
   `mg.<domain>`, publish the Mailgun-generated SPF (TXT), DKIM (TXT), and tracking
   CNAME (+ optional MX) records in Cloudflare and wait for Mailgun to mark the
   domain **verified**. Sequence the SMTP cutover *after* this verifies.
4. **SMTP credentials stored in 1Password** (per-domain SMTP login
   `postmaster@mg.<domain>` + generated password).

## Config wiring (already staged in this repo)

- `infra/terraform/environments/production/compose/docker-compose.blogs.yml` — each
  of the 4 Ghost services has a Mailgun SMTP `mail__*` block reading per-tenant env
  vars, and `security__staffDeviceVerification` is gated on
  `${GHOST_STAFF_DEVICE_VERIFICATION:-false}` (defaults false).
- `infra/terraform/environments/production/cloud-init-blogs.yaml.tpl` — templates
  the SMTP env vars into the droplet `/etc/grove-blogs/.env`.
- `infra/terraform/environments/production/variables.tf` / `blogs.tf` — `ghost_smtp`
  (sensitive, per-tenant user/pass/from), `ghost_smtp_host`, `ghost_smtp_port`,
  `ghost_staff_device_verification`. Empty stub creds keep `plan` working and leave
  the transport **inert** (no regression) until cutover.
- `.env.example` (Odoo) — Mailgun SMTP defaults for order confirmations.

> The droplet backup script `. /etc/grove-blogs/.env` (bash `source`), so injected
> values must be shell-safe: Ghost `from` is a **bare address** (no display name);
> Mailgun SMTP passwords are alphanumeric. Do not add spaces/`<>`/`$` to these.

## ⚠ Provisioning state + cutover-execution constraint (GOL-517, 2026-08-05)

**Provisioning is DONE and verified** (Josh upgraded Mailgun off the free tier —
"we approved and paid for upgraded account"):

- 4 sending domains exist and are `state=active` with **SPF (TXT) + DKIM (TXT)
  valid**: `mg.goldberrygrove.farm`, `mg.woodworkingeorge.com`,
  `mg.atthegrovenursery.com`, and **`send.gatheringatthegrove.com`** — note the
  hub domain was provisioned as `send.*`, not `mg.*`. The `ghost_smtp.from`/`user`
  values absorb this (both are within the authenticated `send.` domain), so **no
  config change is needed**; do not "fix" it to `mg.*`.
- A uniform `grove-tx@<domain>` SMTP credential exists on all 4 domains, password
  reset + **validated live via SMTP AUTH on smtp.mailgun.org:587** (2026-08-05).
  The assembled `ghost_smtp` JSON must be placed by an admin into
  `Grove Infra/ghost_smtp_tf_json` (op service account is read-only on the vault).

**The step-2 `terraform apply` below is NOT a `.env` refresh in the current
state — it is a full droplet REPLACE.** `grove-prod-blogs` has a pending replace
(`user_data` + `monitoring` are ForceNew and already drifted; see
`infra/terraform/environments/production/README.md`). DigitalOcean cannot update
`user_data` in place, so **any** apply touching the droplet destroys and rebuilds
the live box serving all 4 brand blogs — an unscheduled outage on an
unproven-boot droplet (GOL-385). There is **no `-target` that avoids this.** Pick
one execution path:

- **Path A — bundle into the planned droplet replace (codified, durable).** Take
  the SMTP cutover inside the GOL-385 reserved-IP / droplet-replace maintenance
  window (`docs/RUNBOOK-blogs-reserved-ip-cutover.md`). The rebuilt droplet's
  `user_data` renders the live mail block from `TF_VAR_ghost_smtp`. Requires the
  1P blob (above) + a chosen outage window. This also clears the monitoring gap.
- **Path B — interim in-place update (no outage, uncodified).** SSH to the live
  droplet, edit `/etc/grove-blogs/.env` to add the 4 SMTP creds, then
  `docker compose up -d --force-recreate` the 4 Ghost containers. Email works
  immediately with no droplet replace. Caveat: it diverges from state and a later
  droplet replace silently reverts it unless `TF_VAR_ghost_smtp` is populated
  (Path A wiring is already in `.env.op`). Keep `staffDeviceVerification` false
  until SMTP is confirmed delivering, to avoid staff-login lockout.

Either way, populating `ghost_smtp` while `staffDeviceVerification` stays **false**
is the safe first stage: magic links + order receipts start flowing and staff
logins are unaffected. Flip device verification only after that is confirmed.

## Cutover steps (once prerequisites clear — read the constraint above first)

1. Populate `TF_VAR_ghost_smtp` (from 1Password) and set
   `TF_VAR_ghost_staff_device_verification=true`.
2. `terraform apply` the production `blogs` stack — **in the current state this
   REPLACES the droplet** (see constraint above); recreates the droplet `.env`
   and restarts the Ghost containers with the live mail block.
3. **Verify Ghost:** staff login on each instance sends a device-verification code;
   trigger a member magic link. Confirm delivery + `mg.<domain>` in the auth
   headers (SPF=pass, DKIM=pass).
4. **Odoo:** set `SMTP_*` / `EMAIL_FROM` / `FROM_FILTER` from 1Password (Mailgun
   hub sending domain), restart Odoo, and place a test storefront order to confirm
   the order-confirmation email delivers and passes SPF/DKIM.

## Notes / follow-ups

- **Odoo per-brand From:** Mailgun rejects a From outside the authenticated domain,
  so the single Odoo relay sends all order confirmations from the hub sending
  domain. Per-brand From addresses would need one `ir.mail_server` record per
  `mg.<domain>` (each with its own SMTP creds + `from_filter`) — track as a
  follow-up if brand-specific receipt From is required.
- **Region:** defaults assume Mailgun US (`smtp.mailgun.org`). Switch
  `ghost_smtp_host` → `smtp.eu.mailgun.org` if the account is EU.
