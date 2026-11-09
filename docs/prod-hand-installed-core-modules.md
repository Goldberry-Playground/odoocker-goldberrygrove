# Prod Odoo: hand-installed core modules (registry)

Odoo modules reach the production database by one of two paths:

| Path | What it covers | Where it is recorded |
|---|---|---|
| **`AUTO_UPGRADE_MODULES`** (odoo service `environment:` in `infra/terraform/environments/production/compose/docker-compose.odoo.yml`) | The Grove custom modules from grove-odoo-modules plus their dependencies. `odoo/entrypoint.sh` runs `--init=base,<list> --update=<list>` on every modules-revision advance (Leg B of a promote), so these install, upgrade and migrate without a click. | This repo (compose) + the deployed `/etc/grove/docker-compose.yml` on the droplet, converged by `scripts/prod-modules-promote.sh` / `scripts/prod-discord-events-apply.sh`. |
| **Hand-installed core modules** | Stock Odoo 19 CE apps Josh turns on through *Apps* because a feature needs them and no Grove module depends on them. Nothing in this repo installs them; they persist in the prod DB (`ir_module_module.state = installed`) and are upgraded only when a Grove module that depends on them is upgraded, or by hand. | **This file.** Until GOL-3831 the only record was chat. |

This registry exists so a rebuilt prod (restore the DB, boot from cloud-init)
can be checked against a list instead of memory, and so nobody adds a Grove
module dependency on one of these without noticing it is not in the automatic
path. Keep it in sync when a module is installed or uninstalled by hand.

## Registry

| Module | Installed | By | Why | Notes |
|---|---|---|---|---|
| `survey` | 2026-10-26 | Josh, via Apps | Survey forms for the farm/nursery. | Precedent cited by GOL-3828 for how core apps get installed on prod. |
| `google_calendar` | pending, before Train #5 promote (Wed 2026-11-18) | Josh, via Apps (GOL-3828, phase A of grove-odoo-modules#395) | Two-way sync between Josh's primary Google calendar and `calendar.event`, which `grove_calendar` reads to publish public events to Discord. | Needs the OAuth client id/secret from the goldberrygrove.farm Workspace pasted into Settings > Integrations > Google Calendar, then *Sync with Google* in the Calendar app. `grove_calendar` must NOT depend on it (QA and tests install without Google). The `google_calendar` module itself is only installed on prod; QA never pairs a Google account. |

## How to check prod against this list (read-only)

Any prod XML-RPC login that can read `ir.module.module` works; the
`Odoo - Prod Drift Readonly` 1Password item (`Grove Prod`) is the designated
read key.

```python
# python3, after `op run --env-file=scripts/newsletter-mail-servers.prod.env.op -- python3 -`
import os, xmlrpc.client
url, db = os.environ["ODOO_XMLRPC_URL"], os.environ["ODOO_DB"]
uid = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common").authenticate(db, os.environ["ODOO_LOGIN"], os.environ["ODOO_API_KEY"], {})
m = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
for mod in ("survey", "google_calendar", "calendar", "grove_calendar"):
    rows = m.execute_kw(db, uid, os.environ["ODOO_API_KEY"], "ir.module.module", "search_read",
                        [[["name", "=", mod]]], {"fields": ["state", "installed_version"]})
    print(mod, rows[0]["state"] if rows else "<absent from addons_path>", rows[0]["installed_version"] if rows else "")
```

Expected after Train #5 promote: `survey installed`, `google_calendar installed`
(phase A), `calendar installed` (pulled in by `grove_calendar`),
`grove_calendar installed` (Leg B `--init`).

## Rules

- A core module a Grove module **depends on** does not belong here: list the
  Grove module in `AUTO_UPGRADE_MODULES` and `--init` resolves the dependency
  (that is how `calendar`, `im_livechat`, `crm`, `mass_mailing` arrive).
- A core module installed by hand on **QA** is lost at the next train-up
  (QA is destroyed Thursdays). If QA needs it, put a Grove dependency on it or
  add it to the QA compose `AUTO_UPGRADE_MODULES` list instead of this file.
- Uninstalling one of these is a data-destroying Odoo action (it drops the
  module's tables). Board approval, backup first, then strike the row here.
