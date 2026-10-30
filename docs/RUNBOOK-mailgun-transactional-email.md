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
| hub       | gatheringatthegrove.com  | `send.gatheringatthegrove.com` ⚠   |
| goldberry | goldberrygrove.farm      | `mg.goldberrygrove.farm`            |
| ggg       | woodworkingeorge.com     | `mg.woodworkingeorge.com`           |
| nursery   | atthegrovenursery.com    | `mg.atthegrovenursery.com`          |

Keeping this stream on `mg.<domain>` isolates it from (a) the bulk-newsletter
reputation and (b) the human Gmail/Workspace mail on the apex, so a bulk
reputation dip cannot break magic links or receipts.

⚠ **Hub is `send.`, not `mg.`** `mg.gatheringatthegrove.com` is registered to a
Mailgun account we do not control and carries live DNS. Never edit or delete the
`mg.gatheringatthegrove.com` records (see `var.ghost_smtp` in
`infra/terraform/environments/production/variables.tf`).

## Bulk newsletter sending domains — `news.<domain>` (GOL-3455, 2026-10-19)

Josh ruled on 2026-10-19 that Odoo Email Marketing (`mass_mailing`) sends the
newsletters (option C, GOL-3454). Bulk mail gets its own Mailgun sending domain
per brand. Order and transactional mail keep the domains above, so a bulk
reputation dip cannot hurt receipts or magic links.

| Tenant    | Bulk sending domain            | DKIM selector |
|-----------|--------------------------------|---------------|
| hub       | `news.gatheringatthegrove.com` | `pic`         |
| goldberry | `news.goldberrygrove.farm`     | `mx`          |
| ggg       | `news.woodworkingeorge.com`    | `mailo`       |
| nursery   | `news.atthegrovenursery.com`   | `mx`          |

State verified live on 2026-10-19 (same Mailgun account, US region):

- `POST /v4/domains`: 2048-bit DKIM, `spam_action=disabled`, `web_scheme=https`.
  Mailgun reports all four domains `active`.
- Cloudflare records are DNS-only (not proxied), TTL 300, and carry the comment
  `GOL-3455 Mailgun bulk newsletter domain`: the SPF TXT
  `v=spf1 include:mailgun.org ~all`, the DKIM TXT `<selector>._domainkey.news.<domain>`,
  and MX `mxa`/`mxb.mailgun.org` (priority 10) so bounces route back to Mailgun.
  SPF, DKIM and MX are `valid` on all four domains.
- **Tracking is off** (open, click and unsubscribe) on all four domains, and the
  tracking CNAME `email.news.<domain>` is not published on purpose. Odoo
  `mass_mailing` does its own link tracking and unsubscribe handling. Mailgun
  click rewriting or its unsubscribe footer would duplicate that and could break
  Odoo's per-recipient unsubscribe links.
- Apex DMARC covers the subdomain. Mail is DKIM-signed `d=news.<domain>`, which
  aligns (relaxed) with a `From:` on `news.<domain>`.

**SMTP credentials** use the login `newsletter@news.<domain>`, stored on 1P
`Goldberry Grove - Admin` → `Mailgun | Goldberry Grove` (item
`7t2pap33hu37bftpsy7uzmakuy`) as `smtp_login_news_<tenant>` and
`smtp_password_news_<tenant>`, following the `smtp_login_<tenant>` pattern. The
ops service account is read-only on that vault, so a human mints and stores
them — one command, dry-run first:

```bash
scripts/mailgun-mint-news-smtp-creds.sh            # plan only
scripts/mailgun-mint-news-smtp-creds.sh --apply    # mint, store on 1P, re-read, SMTP AUTH test
```

It creates (or, on re-run, resets) each Mailgun login, writes both 1P fields,
re-reads them (`op item edit` can print a spurious error even when the write
lands, so the re-read is the source of truth) and AUTHs on
`smtp.mailgun.org:587` STARTTLS with the stored values. Nothing secret is echoed.

**Odoo wiring (codified, GOL-3455 §4).** One `ir.mail_server` per brand,
`from_filter=news.<domain>`, host `smtp.mailgun.org:587` STARTTLS, declared in
`scripts/newsletter-mail-servers.spec.json` and applied with the existing
idempotent reconfig tool:

```bash
op run --env-file=scripts/newsletter-mail-servers.env.op -- \
  python3 scripts/prod-reconfig.py --target qa \
    --spec scripts/newsletter-mail-servers.spec.json --apply
```

Why the shape is what it is:

- Odoo 19 picks the outgoing server by matching the From domain against
  `from_filter`; when nothing matches it falls back to the **lowest-sequence**
  server and rewrites the From. The transactional row stays at sequence 5 and
  the `news.` rows sit at 20–23, so an unmatched From (odoobot, a user's own
  address) still goes out transactional and never through a bulk domain. The
  spec's assertions fail the run if a bulk row ever sits below 20 or the
  transactional row goes missing.
- `ir.mail_server` has no company field in Odoo 19, so "per company" means
  per From domain: a Farm News mailing is sent From `…@news.goldberrygrove.farm`.
- `mass_mailing.outgoing_mail_server=True` turns on the per-mailing **Send From**
  server picker in Email Marketing. There is deliberately no single default
  `mass_mailing.mail_server_id` — four brands, one list of record.
- QA and prod share the domains and credentials. A QA→prod DB promotion carries
  the rows over, so prod needs `--check`, not `--apply`.

Run order on a train: mint creds (above) → `--apply` on QA on the Monday
bring-up → compose a test mailing per brand From `news.<domain>` and confirm the
`Received:` chain shows `d=news.<domain>` → `--check` on prod after the Wednesday
promote. See the matching gate in `RUNBOOK-release-train.md`.

**Rollback:** `DELETE /v3/domains/news.<domain>` in Mailgun, then delete the
Cloudflare records carrying the `GOL-3455` comment. Neither touches the
transactional domains.

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
- A uniform `transactional@<domain>` SMTP credential exists on all 4 domains,
  password reset + **validated live via SMTP AUTH on smtp.mailgun.org:587**
  (hub re-verified 2026-08-18 for GOL-995). Per-domain login/password are in
  1Password `Goldberry Grove - Admin` → `Mailgun | Goldberry Grove` as
  `smtp_login_{hub,goldberry,ggg,nursery}` / `smtp_password_{...}` (hub =
  `transactional@send.gatheringatthegrove.com`). The assembled `ghost_smtp`
  JSON must be placed by an admin into `Grove Infra/ghost_smtp_tf_json` (op
  service account is read-only on the vault).

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

## Tracking hosts over HTTPS — `email.<sending-domain>` (GOL-3594, 2026-10-22)

The storefronts send HSTS. Adding `includeSubDomains` means every subdomain of
each brand apex must complete a TLS handshake. That includes Mailgun's
tracking hosts. State as of 2026-10-22:

| Tracking host | DNS (Cloudflare) | Cert |
| --- | --- | --- |
| `email.mg.goldberrygrove.farm` | CNAME `mailgun.org`, **DNS-only** | Mailgun-issued Let's Encrypt |
| `email.mg.atthegrovenursery.com` | CNAME `mailgun.org`, DNS-only | Mailgun-issued Let's Encrypt |
| `email.mg.woodworkingeorge.com` | CNAME `mailgun.org`, DNS-only | Mailgun-issued Let's Encrypt |
| `email.send.gatheringatthegrove.com` | CNAME `mailgun.org`, DNS-only | Mailgun-issued Let's Encrypt |

All four Mailgun domains have `web_scheme=https`. The `news.<domain>` domains
were created with `web_scheme=https` and have no `email.news.*` record (tracking
is off), so they have no tracking host to cover.

**Rules:**

- **Never proxy (orange-cloud) an `email.<sending-domain>` CNAME.** Universal
  SSL covers `*.<apex>` only. A third-level name like `email.mg.<apex>` gets no
  edge cert, so the handshake fails at Cloudflare. Mailgun also cannot complete
  its Let's Encrypt HTTP-01 challenge through the proxy.
- **Adding a new sending domain:** create it with `web_scheme=https`, publish
  the tracking CNAME as DNS-only, and run `POST /v2/x509/email.<domain>`. Then
  poll `GET /v2/x509/email.<domain>/status` until it reads `active`. Mailgun
  auto-renews the cert.
- The hub sends from `send.gatheringatthegrove.com`, not `mg.*`. There is no
  `mg.gatheringatthegrove.com` Mailgun domain. Its proxied `email.mg` CNAME was
  an orphan and was deleted 2026-10-22 (the content was `mailgun.org`, proxied).
  Its stale SPF and DKIM TXT records under `mg.gatheringatthegrove.com` are
  harmless and were left in place.
- Open, click and unsubscribe tracking stays **off** on all domains, because
  click rewriting breaks Ghost magic links. HTTPS is about correctness under
  HSTS, not about turning tracking on.

**Verify:** `curl -sI https://<host>/`. Any HTTP status (Mailgun answers
`404` on `/`) means TLS works. A handshake error means it does not.

## Notes / follow-ups

- **Odoo per-brand From:** Mailgun rejects a From outside the authenticated domain,
  so the single Odoo relay sends all order confirmations from the hub sending
  domain. Per-brand From addresses would need one `ir.mail_server` record per
  `mg.<domain>` (each with its own SMTP creds + `from_filter`) — track as a
  follow-up if brand-specific receipt From is required.
- **Region:** defaults assume Mailgun US (`smtp.mailgun.org`). Switch
  `ghost_smtp_host` → `smtp.eu.mailgun.org` if the account is EU.
