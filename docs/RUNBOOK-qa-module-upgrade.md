# RUNBOOK — QA Odoo module upgrade (`-u <module>`)

**Owner:** DevOps-Terra · **Runner:** whoever holds SSH to the L3 QA droplet
(port 22 is firewalled to the **admin IP** — see below). · **Env:** L3 QA
(`odoo.qa.gatheringatthegrove.com`).

## Why this exists

The L3 QA Odoo droplet delivers `grove-odoo-modules` code via a **git-sync
sidecar** (`custom-modules-sync`, tracking `main`), *not* via a baked image.
git-sync brings **code** onto the droplet within `GITSYNC_PERIOD` (60s), but
Odoo does **not** re-run a module's migrations or bump its installed version
from code alone.

The `qa` branch of the image entrypoint (`odoo/entrypoint.sh`) boots with
`--init=base` and **deliberately no `--update`** — QA testers expect module +
data state to persist across `restart: unless-stopped` bounces. Therefore:

- A container **restart** does **not** upgrade a module.
- A **droplet replace** does **not** either — the DB lives on DO Managed
  Postgres (durable across replaces), so a fresh droplet boots `--init=base`
  against the already-installed module and no-ops.

**The only way to run a module's new migrations + register new fields on QA is
an explicit `-u <module>` pass.** This runbook is that pass.

> Example that triggered this runbook: `grove_headless` **19.0.1.14.0** (PR #51)
> adds the `grove_guide_ready` Boolean + `website_description` gating on
> `product.template` with migration `19.0.1.14.0`. The code git-synced to QA on
> merge, but the field/migration only take effect after `-u grove_headless`.

## Access constraint (why this is not a CI job)

Port 22 on the L3 droplets is scoped by firewall to the **admin IP** only
(config goes IN via cloud-init; app deploys via image pull — the L3 pattern,
see `release.yml` header). GitHub Actions runners and Paperclip agent runtimes
are **not** on the admin IP, so this cannot (today) be a `workflow_dispatch`
job — it is run from an operator machine that holds the admin IP + the
`grove-qa` SSH key. If/when a bastion or IP-allowlisted runner exists, wrap
`scripts/qa-module-upgrade.sh` in a dispatch workflow and delete this caveat.

## Provenance: always pass `EXPECT_REF` for a release-train pass

QA **floats `main`** (`custom_modules_ref` defaults to `main` — see
`infra/terraform/environments/qa-app-platform/variables.tf`), so git-sync moves
the checkout under us. "I ran the upgrade" therefore never said *which commit*
was upgraded, and an `-u` against a stale or mid-pull checkout silently gates
the wrong code — the failure mode that matters most when a promote's money path
is riding on the gate.

The script resolves the synced commit before upgrading (git-sync sidecar
`git rev-parse HEAD`, falling back to the `/workspace/current` symlink target,
which git-sync names by commit) and prints it along with each module's on-disk
manifest version. Pass `EXPECT_REF=<40-char sha>` to make it **fail closed**:

| Situation | Behaviour |
|---|---|
| `EXPECT_REF` matches the synced commit | proceeds, echoes the SHA |
| `EXPECT_REF` set, synced commit differs | **exit 3**, no upgrade (QA has not pulled it yet, or is mid-pull — wait a sync period and re-run) |
| `EXPECT_REF` set, commit unresolvable | **exit 3**, refuses to upgrade blind |
| `EXPECT_REF` unset | warns, then upgrades whatever git-sync last pulled |
| `EXPECT_REF` not a 40-char lowercase hex SHA | **exit 2** locally, before touching the droplet |

## Procedure (idempotent, re-runnable)

From the **admin machine** (has SSH to the droplet):

```bash
# Release-train / money-path pass — pin the commit you intend to gate:
EXPECT_REF=cf519bfa8ee59c493fc94eee15d77a504e7a09a7 \
  scripts/qa-module-upgrade.sh grove_headless

# Casual QA pass (whatever main is right now):
scripts/qa-module-upgrade.sh grove_headless
```

or, inline without the repo checked out:

```bash
ssh root@odoo.qa.gatheringatthegrove.com '
  set -euo pipefail
  cd /etc/grove
  set -a; . ./.env; set +a
  # Run the migration/upgrade pass (exits after init; server is down briefly).
  docker compose --env-file /etc/grove/.env exec -T odoo \
    odoo -d "$DB_NAME" -u grove_headless --stop-after-init --no-http --workers=0
  # Bring the normal (long-running) server back up.
  docker compose --env-file /etc/grove/.env restart odoo
  echo "QA upgrade of grove_headless complete."
'
```

`-u <module>` is safe to run twice: Odoo re-applies the module state and skips
migrations already recorded in `ir_module_module`. Downtime is only the
`--stop-after-init` window (seconds to low minutes) plus the restart.

## Verify

0. **Installed version bumped** — the single check that proves the migrations
   ran (git-sync alone leaves this at the *old* version while the code on disk
   is already new). From anywhere with the QA admin API key:

   ```bash
   python3 - <<'PY'
   import os, xmlrpc.client
   u, db = "https://odoo.qa.gatheringatthegrove.com", "odoo"
   key = os.environ["ODOO_API_KEY"]  # op://Grove QA/<qa odoo item>/odoo_mcp_qa_api_key
   uid = xmlrpc.client.ServerProxy(u + "/xmlrpc/2/common").authenticate(
       db, os.environ["ODOO_LOGIN"], key, {})
   m = xmlrpc.client.ServerProxy(u + "/xmlrpc/2/object")
   print(m.execute_kw(db, uid, key, "ir.module.module", "search_read",
                      [[["name", "=", "grove_headless"]]],
                      {"fields": ["name", "state", "installed_version"]}))
   PY
   ```

   `installed_version` must equal the manifest version the script printed
   pre-upgrade. If it still shows the old version, the `-u` did not take.

1. **In the upgrade log** (stdout of the `-u` step): look for
   `Modules loaded.` and, on a version bump, a
   `module grove_headless: upgrading to version 19.0.1.14.0` line with no
   tracebacks. Exit code 0.
2. **Field registered** (needs QA admin API key —
   `op://Grove QA/Gather At the Grove QA Odoo/odoo_mcp_qa_api_key`):

   ```bash
   # JSON-RPC: grove_guide_ready must exist on product.template
   curl -s https://odoo.qa.gatheringatthegrove.com/jsonrpc -H 'Content-Type: application/json' -d '{
     "jsonrpc":"2.0","method":"call","params":{"service":"object","method":"execute_kw",
     "args":["odoo",2,"<API_KEY>","ir.model.fields","search_count",
       [[["model","=","product.template"],["name","=","grove_guide_ready"]]]]}}'
   # → {"result": 1}  means the migration registered the field.
   ```
3. **Functional** (downstream GOL-910 GuideBlock): a product with
   `grove_guide_ready = true` returns a non-null `website_description` from
   `/grove/api/v1/products/{id}`; an unapproved one returns null.

## Rollback

`grove_headless` migrations are additive (new nullable field, default False).
To revert code, repoint `CUSTOM_MODULES_REF` to the prior pin and re-sync; the
already-added column is inert when the older controller ignores it. No data
loss path here — do **not** attempt to drop the column on QA.
