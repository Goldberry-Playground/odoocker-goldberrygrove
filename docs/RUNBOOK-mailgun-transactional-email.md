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

## Cutover steps (once prerequisites clear)

1. Populate `TF_VAR_ghost_smtp` (from 1Password) and set
   `TF_VAR_ghost_staff_device_verification=true`.
2. `terraform apply` the production `blogs` stack — recreates the droplet `.env`
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
