# Runbook — Otto `ODOO_API_KEY` write-back vault (`Grove CI Writeback`)

**Owner:** DevOps-Terra · **Tickets:** GOL-237 (this write path), GOL-89 (Otto go-live)
**Status:** vault + token NOT yet created. Required before the first LIVE run of
`.github/workflows/provision-logistics-otto.yml`. NOT required for the Infisical
teardown (GOL-231) — that only needed the Infisical reference gone, which it is.

## What this is

`provision-logistics-otto.yml` mints a least-privilege Odoo API key for the
`logistics-otto` user and must persist it somewhere durable so DevOps-Terra can
inject it into Otto's Paperclip env. It is the **only CI step in this repo that
writes a secret** — everything else reads via `1password/load-secrets-action`,
which is read-only.

## Why a dedicated vault and not `Grove Prod`

The obvious move — grant `OP_CI_SA_TOKEN`'s service account `write_items` on
`Grove Prod` — is **rejected**. That token is used by `terraform-drift`,
`qa-health`, `infracost`, and `ci-failure-notify`; making it writable would let
any of those runs overwrite **any** prod secret (DigitalOcean, Cloudflare, Spaces
tokens). That is strictly *worse* than the Infisical per-path write scope it
replaced — we'd be regressing security to finish a migration.

Instead: a vault whose entire contents are keys CI mints itself.

| | Blast radius of a leaked/abused write token |
|---|---|
| `write_items` on `Grove Prod` | every prod credential — DO, Cloudflare, Spaces, Discord |
| `write_items` on `Grove CI Writeback` | the Otto Odoo keys, which are Inventory+Purchase+Sales+UoM scoped and re-mintable by re-running the workflow |

## Setup (1Password admin — Josh; ~5 min, console + CLI)

1. **Create the vault**, named exactly `Grove CI Writeback`.
   Description: *"Write-only target for CI-minted secrets (Otto Odoo API keys). Not a general secret store — do not add anything a workflow does not itself mint."*

2. **Create a service account** named `grove-ci-writeback`, granted on
   **`Grove CI Writeback` only**:
   - `write_items`, `read_items` (the workflow reads to decide create-vs-edit)
   - **no** access to `Grove Prod`, `Grove QA`, or `Goldberry Grove - Admin`

   ```bash
   op service-account create grove-ci-writeback \
     --vault "Grove CI Writeback:read_items,write_items"
   ```

3. **Seed the token as a repo secret** named `OP_CI_WRITEBACK_SA_TOKEN`
   (`Goldberry-Playground/odoocker-goldberrygrove` → Settings → Secrets →
   Actions). Repo-level, not environment-level — matching `OP_CI_SA_TOKEN`.

4. **Verify without a deploy** — dispatch `otto-writeback-verify.yml` (see
   below). It round-trips a canary through the vault and asserts the token can
   write but cannot reach `Grove Prod`. No Odoo, no droplet, no prod deploy.

## Verification: `otto-writeback-verify.yml`

Temporary workflow, `workflow_dispatch` only. It:

1. writes a canary value to `op://Grove CI Writeback/_ci-writeback-canary/value`
2. reads it back and asserts it round-tripped (presence check — the value is
   masked and never printed)
3. asserts the token is **denied** on `Grove Prod` (proves the scope is tight,
   not just that it works)
4. deletes the canary item

**Delete this workflow once Otto goes live** (GOL-89) — it exists to prove the
write path before there's a real key to risk, not to live in CI forever.

## Residual risks (accepted, documented)

- **qa/prod share one vault + token.** 1Password SA tokens scope per-vault, not
  per-item, so a qa run holds a token that could technically write the
  `otto-prod` item. The workflow computes the item name from its `environment`
  input, so this can't happen in normal operation. Worst case is availability,
  not disclosure: Otto's prod env would hold a qa key until the prod workflow is
  re-run. If we later want true env isolation, split into
  `Grove CI Writeback QA` / `Grove CI Writeback Prod` with two tokens — costs one
  extra vault + one extra repo secret.
- **The minted key transits the runner's argv.** `op item edit` takes field
  values as arguments, so the key is briefly visible in the runner's process
  list. GitHub-hosted runners are ephemeral and single-tenant per job, so there
  is no co-tenant to observe it. The value is `::add-mask::`'d before this point,
  so it cannot reach logs.

## Rollback

The write step is the last step that touches durable state; nothing downstream
depends on it within the run. If the write fails, the run fails, the minted key
is discarded, and the prior key in the vault is untouched — Otto keeps working on
its existing key. Re-running the workflow re-mints and re-writes cleanly (the
mint script revokes any prior same-name key first).
