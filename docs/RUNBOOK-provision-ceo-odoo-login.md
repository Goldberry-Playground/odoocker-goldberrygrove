# Runbook — Provision the CEO's prod Odoo login (GOL-1780)

**Problem.** Josh (CEO) has **no `res.users` login** on prod Odoo. Only API
service keys (`odoo_api_keys_tf_json`), DB credentials, and the master/DB
password exist in 1Password — none of which is a human login.
`https://odoo.gatheringatthegrove.com/web/login` is therefore unusable by him,
which blocks every catalog-data fix (GOL-408 nursery photos, `$0.00` prices).

**Why a shell script and not XML-RPC / a stored password.** Bootstrapping the
*first* admin user is chicken-and-egg: XML-RPC needs an existing `res.users`
login to authenticate as, and there is none. The only admin secret on the
droplet is the Odoo **master password** (`odoo.conf admin_passwd`), which gates
`/web/database` management — it is not a user credential. Inside
`odoo shell`, `env` runs as **superuser**, so no credential is needed. This is
the identical mechanism `provision-logistics-otto` (GOL-89) uses and the board
ratified.

**Credential handling.** Terra never mints, prints, or stores a plaintext
password. The script emits a **one-time set-password link** (`auth_signup`) or,
in CI, triggers Odoo to **email** that link to Josh. Josh sets his own password
and saves it in 1Password (**Grove Prod**) himself.

## Rights granted (CEO / admin)

| Group | Purpose |
|-------|---------|
| `base.group_user` | Internal User (mandatory base) |
| `base.group_system` | Administration / Settings — full admin config, the "Settings access" posture GOL-1780 asks about; correct for a solo-operator CEO |
| `base.group_erp_manager` | Access Rights |
| `sales_team.group_sale_manager` | Sales / Administrator |
| `stock.group_stock_manager` | Inventory / Administrator (adjustments, product editing) |
| `purchase.group_purchase_manager` | Purchase / Administrator |
| `account.group_account_manager` | Accounting (if installed — optional) |
| `website.group_website_designer` / `…restricted_editor` | Website / eCommerce editing (if installed — optional) |

Optional groups whose module isn't installed are skipped with a WARN, never
fatal. `base.group_user` + `base.group_system` are required; the script aborts
if either is missing (wrong DB / modules).

## Company scoping (GOL-1842 / GOL-1811)

On a multi-company DB, a user with no explicit `company_ids` is left on whatever
default the DB picks, so records owned by the other LLC companies are invisible
(the GOL-1811 symptom). The script therefore resolves the intended company set
and sets **both**:

- `company_ids` (allowed) — additive `(4, cid)` links; never drops an existing
  allowed company.
- `company_id` (active) — always forced to be within the allowed set (Odoo
  constraint).

Defaults: **all** companies in the DB are allowed (a solo-operator CEO owns every
entity), and the active company is the user's current one if already allowed,
else the lowest-id (main) company. Override per-run with container env:

- `CEO_COMPANIES="Goldberry Grove Farm;At The Grove Nursery, LLC"` — restrict the
  allowed set to those exact `res.company` names, **semicolon**-separated (a name
  may contain a comma). Unknown names WARN + skip.
- `CEO_MAIN_COMPANY="Goldberry Grove Farm"` — pick the active company by name.

The live run prints `CEO_ODOO_COMPANY_IDS=` and `CEO_ODOO_ACTIVE_COMPANY_ID=`;
the dry-run prints the current vs resolved company sets so you can review before
writing. NOTE: re-running the OLD script does NOT fix company scoping — it was
idempotent on groups only; this version is what actually sets the companies.

## Google SSO?

**Not available today.** `auth_oauth` is not installed or configured on prod
Odoo, and there is no Google OAuth client registered for it. Enabling SSO is a
separate provisioning task (install `auth_oauth`, register a Google Cloud OAuth
client, configure the provider record) — not on the 08-28 launch path. Use the
local login below now; SSO can be a follow-up if the board wants it.

---

## Path A — CI (preferred: one click, gated, no secret on your terminal)

Requires prod SMTP to be wired (so Odoo can email the link).

1. GitHub → **Actions** → **Provision CEO Odoo Login** → **Run workflow**.
2. First run with **`dry_run: true`** — reviews the resolved groups and prints
   the SMTP / `auth_signup` posture. Approve the `production` environment when
   prompted (this is the CEO gate). Confirm the preview looks right.
3. Re-run with **`dry_run: false`**. Approve `production` again. On success the
   job summary reads *"Odoo emailed Josh a one-time set-password link."*
4. Open the email → set your password → **save it in 1Password → Grove Prod**
   (suggested item: `Josh — Odoo Prod Login`, category *Login*, url
   `https://odoo.gatheringatthegrove.com/web/login`).

If **no email arrives**, prod Odoo SMTP is not wired — use Path B.

## Path B — Manual SSH (no email dependency; link prints to your terminal)

Run from a machine that holds the prod deploy SSH key (`PROD_HOST` droplet).

The live prod droplet runs a **single** `docker-compose.yml` under **`/etc/grove`**
— there is no `/opt/grove`, and no `docker-compose.override.*` files (confirmed
off the running container's own compose labels, 2026-08-31). `.env` (which
defines `DB_NAME`) is at `/etc/grove/.env` and the service is `odoo`.

```bash
# From a checkout of odoocker-goldberrygrove at the repo root:
ssh -o StrictHostKeyChecking=yes "root@${PROD_HOST}" \
  'set -a; . /etc/grove/.env; set +a; \
   cd /etc/grove && docker compose \
     exec -T odoo odoo shell -d "$DB_NAME" --no-http --logfile=/dev/null' \
  < scripts/provision_ceo_user_shell.py
```

- **Dry-run first:** insert `-e CEO_DRY_RUN=1` right after `exec -T` to preview
  without writing.
- Live run prints a block:
  - `----BEGIN CEO_SET_PASSWORD_URL----` … open it once, set your password; **or**
  - `----BEGIN CEO_TEMP_PASSWORD----` … (only if `auth_signup` is absent) log in,
    change it immediately under **Preferences → Account Security**.
- Then **save the password in 1Password → Grove Prod** (see item suggestion above).
- The link/temp password appears **only on your terminal, once** — do not paste
  it into any issue or chat.

## Verify

```
curl -sS -o /dev/null -w '%{http_code}\n' https://odoo.gatheringatthegrove.com/web/login
```

Then log in with `josh@goldberrygrove.farm`; you should land on the backend with
the **Settings** app visible (confirms `base.group_system`). You can now upload
nursery product photos and correct `$0.00` prices (GOL-408).

Confirm the **company scoping** actually landed (GOL-1811 was this being unset).
The live provision output already echoes `CEO_ODOO_COMPANY_IDS=` /
`CEO_ODOO_ACTIVE_COMPANY_ID=`; to check against prod directly, in the backend the
company switcher (top-right) should list every LLC, or from an Odoo shell:

```
u = env["res.users"].search([("login", "=", "josh@goldberrygrove.farm")])
print(u.company_id.name, [c.name for c in u.company_ids])
```

## Idempotency / re-runs

Safe to re-run. The script finds-or-creates the user, re-ensures the group set
(additive `(4, gid)` links) and the company set (additive `(4, cid)` links +
active `company_id`), and re-issues the set-password link.
