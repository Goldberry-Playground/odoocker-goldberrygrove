# Runbook — Resend SMTP transactional email (GOL-580 / GOL-465 Phase 3)

Moves Odoo's transactional / order-confirmation relay onto **Resend** (ESP
decision by Ada, within the GOL-465 pre-approved spend envelope; **SES is the
fallback**). This supersedes the earlier GOL-245 Mailgun-only plan for the Odoo
storefront stream. Bulk newsletter (Ghost) is **out of scope**.

Transactional spend is ~pennies/mo and fits Resend's free/lowest tier.

## Sending-domain split (why a separate `send.<domain>`)

Transactional mail sends from a dedicated Resend sending subdomain per brand, so
its reputation is isolated from apex/human mail and from bulk newsletter:

| Tenant    | Brand domain            | Resend sending subdomain      |
|-----------|-------------------------|-------------------------------|
| goldberry | goldberrygrove.farm     | `send.goldberrygrove.farm`    |
| nursery   | atthegrovenursery.com   | `send.atthegrovenursery.com`  |
| ggg       | woodworkingeorge.com    | `send.woodworkingeorge.com`   |

> `woodworkingeorge.com` is **single-g** — the live/registered zone. The
> double-g spelling is NOT registered; do not add it.
>
> The hub (`gatheringatthegrove.com`) is intentionally **not** a Resend send
> domain in GOL-580's scope. If hub-branded transactional mail is needed later,
> add `send.gatheringatthegrove.com` as a 4th domain the same way.

## Prerequisites (blocking)

1. **Resend account for Goldberry Grove.** Signup needs a human-owned inbox to
   verify (and a billing card only if prompted — free tier does not require one).
   **This is the one step DevOps cannot self-serve** — hand to Josh. Owner/login
   decision is his/CEO's.
2. **API key + SMTP token stored in 1Password**, NOT in git. Suggested fields on
   `Goldberry Grove - Admin` → a new `Resend | Goldberry Grove` item:
   - `api_key` — Resend API key (`re_...`)
   - `smtp_password` — Resend SMTP token (used as Odoo `SMTP_PASSWORD`)
   (SMTP username is the literal string `resend`; host `smtp.resend.com:587`.)
3. **3 sending domains added in Resend** as `send.<domain>` (table above).
4. **DNS auth records published in Cloudflare + Resend shows "Verified"** for all
   3 — see below.

## DNS (Cloudflare) — codified

Resend generates the exact **DKIM CNAME target** and **feedback-smtp AWS region**
at domain-add time, so they can't be pre-known. The deterministic records
(SPF / DMARC) are pre-filled; fill the two Resend-generated values, then apply
idempotently:

```
# 1. Add each send.<domain> in the Resend dashboard; copy the DNS records it shows.
cp scripts/resend-records.example.tsv scripts/resend-records.tsv
#    edit scripts/resend-records.tsv: replace every TODO_FROM_RESEND with the
#    exact value Resend shows (DKIM CNAME target + feedback-smtp region).

# 2. Publish to Cloudflare (idempotent, safe to re-run; DNS:Edit token):
export CLOUDFLARE_API_TOKEN="$(op read 'op://Goldberry Grove - Admin/Grove Infra/account_cloudflare_api_token')"
DRY_RUN=1 scripts/publish-resend-dns.sh scripts/resend-records.tsv   # preview
scripts/publish-resend-dns.sh scripts/resend-records.tsv             # apply

# 3. Wait for propagation, then click "Verify" on each domain in Resend.
scripts/wait-for-dns.sh 300 send.goldberrygrove.farm send.atthegrovenursery.com send.woodworkingeorge.com
```

Per `send.<domain>` the records are: **SPF** `TXT "v=spf1 include:amazonses.com
~all"`, **DKIM** `CNAME resend._domainkey.send.<domain>`, **MX**
`feedback-smtp.<region>.amazonses.com` (bounce/feedback, priority 10), **DMARC**
`TXT _dmarc.send.<domain> "v=DMARC1; p=none;"`. Records are created **unproxied**
(grey cloud). `scripts/resend-records.tsv` is git-ignored-by-convention (contains
only public DNS values, but keep the canonical copy with real values in the vault
note, not necessarily committed).

## Odoo SMTP wiring (already staged in this repo — `.env.example`)

```
SMTP_SERVER=smtp.resend.com
SMTP_PORT=587
SMTP_SSL=False        # STARTTLS on 587. Odoo smtp_ssl=True => implicit TLS (465) => breaks.
SMTP_USER=resend
SMTP_PASSWORD=        # real Resend SMTP token injected from 1Password at deploy
FROM_FILTER=          # EMPTY: multi-domain relay; each company keeps its real From
EMAIL_FROM="Goldberry Grove <notifications@send.goldberrygrove.farm>"  # last-resort fallback only
```

Per-company From is set by `grove_headless` (grove-odoo-modules#25) via
`res.company.email = notifications@<domain>`.

> ⚠️ **From/verified-domain match (flag to Ada):** the From domain must be a
> Resend-**verified** domain. This runbook verifies the `send.<domain>`
> subdomain, so the deliverable From should be `notifications@send.<domain>`
> (matching the verified zone), OR the apex `<domain>` must be verified in Resend
> instead. If #25 sets `notifications@<apex>` while only `send.<apex>` is
> verified, Resend will reject the send. Reconcile before the acceptance test.

## Cutover + verify

1. Land this env change; inject the real `SMTP_PASSWORD` (Resend SMTP token) from
   1Password at deploy. Until then the relay is inert (empty password).
2. Confirm all 3 domains show **Verified** in Resend (DKIM + SPF pass).
3. Ada confirms grove-odoo-modules#25 is deployed, runs a test Nursery order, and
   confirms the confirmation lands **inbox-not-spam** with passing DKIM/SPF —
   that closes GOL-465's "done when".

## Rollback

Revert `.env.example` to the Mailgun block and redeploy; Mailgun creds remain in
the `Mailgun | Goldberry Grove` vault item. DNS `send.<domain>` records are
additive and can be left in place (harmless) or deleted per zone.
