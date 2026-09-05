# Runbook — Rotate or disable the generic Odoo admin uid=2 (GOL-2080)

**Problem.** During the 2026-08-31 CEO prod-Odoo login recovery (GOL-1780) a
fallback password was set on the generic, built-in administrator account
`res.users` **uid=2** to regain access. That is a **shared, non-attributable
superuser credential**. Now that Josh has his own attributable CEO login (a
separate uid with `base.group_system` on all companies), the uid=2 fallback must
not persist as a live interactive credential.

**Why a shell script and not XML-RPC.** You cannot disable an account by
authenticating as it, and the whole point is that uid=2 is a credential nobody
should hold. Inside `odoo shell`, `env` runs as **superuser**, so no login is
needed — the same CEO-ratified mechanism `provision-ceo-odoo-login` (GOL-1780)
and `provision-logistics-otto` (GOL-89) use.

**Credential handling.** Terra never mints, prints, or stores a plaintext
password. Both write modes set a **fresh random password and discard it** — the
value is never printed, logged, or returned. uid=2 is a built-in/service
account, not a login anyone should use, so no human needs the new value. Josh
keeps his own attributable CEO login.

## Decision: disable vs rotate

| Report shows | Action | Why |
|--------------|--------|-----|
| `automation_dependencies_total=0` | **disable** | Nothing runs as uid=2, so archive it (`active=False`) and rotate its password. Strongest: an archived user cannot log in interactively OR over API keys. |
| `automation_dependencies_total > 0` | **rotate** | Something (an API key or a scheduled action) authenticates/runs as uid=2. Archiving would break it, so only rotate the password (kills the known shared secret) and leave the account active. |

`disable` **refuses** (exit 4) if it detects any dependency, so you cannot
accidentally break automation — it tells you to re-run with `rotate`.

GOL-2080 note: the Grove API service keys live under a **separate** service user
(`odoo_api_keys_tf_json`), not uid=2. The `report` mode verifies that assumption
against the live DB (`api_keys_owned` should be `0` for uid=2).

## Guards

- uid=1 (OdooBot / `base.user_root`) is **never** touched.
- The target is refused if uid=2's login is the CEO login (`CEO_LOGIN`, default
  `josh@goldberrygrove.farm`) — we must not disable Josh.
- The target is refused if uid=2 does not resolve to a real `res.users` row.

---

## Path A — CI (preferred: one click, gated)

The `production` GitHub Environment reviewer **is** the CEO approval gate.

1. GitHub → **Actions** → **Rotate or Disable Odoo admin (uid=2)** → **Run
   workflow**.
2. Run with **`mode: report`** first. Approve the `production` environment when
   prompted. Read the `ADMIN2_REPORT` block in the job log:
   - `password_live` — is the fallback still set?
   - `automation_dependencies_total` — decides disable vs rotate (table above).
   - `recommended_action` — the script's own call.
3. Re-run with **`mode: disable`** (if deps == 0) or **`mode: rotate`** (if deps
   > 0). Approve `production` again. On success the job summary reads *"the
   shared fallback password on uid=2 is now invalid."*

Requires `PROD_SSH_PRIVATE_KEY` + `PROD_HOST` to be provisioned (repo or
`production`-environment secrets). If they are not, the "Add droplet to known
hosts" step exits 1 — use Path B.

## Path B — Manual SSH (no repo secrets needed; runs from your terminal)

Run from a machine that holds the prod deploy SSH key (`PROD_HOST` droplet).

The live prod droplet runs a **single** `docker-compose.yml` under **`/etc/grove`**
— no `/opt/grove`, no `docker-compose.override.*` files. `.env` (which defines
`DB_NAME`) is at `/etc/grove/.env`; the service is `odoo`.

```bash
# From a checkout of odoocker-goldberrygrove at the repo root.
# 1) REPORT (read-only) — always run this first.
ssh -o StrictHostKeyChecking=yes "root@${PROD_HOST}" \
  'set -a; . /etc/grove/.env; set +a; \
   cd /etc/grove && docker compose \
     exec -T -e ADMIN2_MODE=report \
     odoo odoo shell -d "$DB_NAME" --no-http --logfile=/dev/null' \
  < scripts/rotate_disable_admin2_shell.py

# 2) Then EITHER disable (deps == 0) …
#    change ADMIN2_MODE=report -> ADMIN2_MODE=disable
# 2') … OR rotate (deps > 0)
#    change ADMIN2_MODE=report -> ADMIN2_MODE=rotate
```

- No secret is printed in any mode. `report` prints an `ADMIN2_REPORT` block;
  the write modes print an `ADMIN2_RESULT` block (state only, no password).
- `disable` sets `active=False` **and** rotates the password. `rotate` only
  rotates the password and leaves `active=True`.

## Verify

After a write run, re-run **`mode: report`** (or Path B report). Confirm:

- `disable`: `active=False`. (An archived user is rejected at login and for API
  auth.)
- `rotate`: `password_live=True` but the value is the new random one nobody
  holds; the old shared fallback no longer works.

Optional interactive check (Josh): browse to
`https://odoo.gatheringatthegrove.com/web/login` and confirm the **old** uid=2
fallback password no longer logs in, while Josh's own CEO login still works.

## Local test (no prod)

The pure decision logic (guards, disable-blocks-on-dependency, rotate-keeps-
active, password shape, read-only report) is covered by:

```bash
python3 scripts/test_rotate_disable_admin2_shell.py
```
