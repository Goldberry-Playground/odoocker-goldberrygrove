# Backup dead-man's switch → self-hosted obs stack (GOL-854)

Board decision `3a05a40b` (Josh, 2026-07-27): the nightly prod backups must have a
dead-man's switch, and it lives on the **self-hosted obs stack** (grove-obs:
OpenObserve + Keep, from GOL-270) — **not Healthchecks.io**, and **not left
unmonitored**. This supersedes the Healthchecks.io monitoring step (step 1) of
GOL-830; that tool choice is overridden. GOL-830 remains the prod-execution /
drift tracker.

## What a dead-man's switch is here

The backup scripts already do the right thing on the *sending* side: they
**heartbeat only on success**. `set -euo pipefail` means any failure in the sync
skips the heartbeat entirely, so a *silent* backup failure is indistinguishable
from a *no-heartbeat* — which is exactly what the monitor watches for. A backup
nobody is watching is not a backup.

This issue delivers the **receiving** side: a monitor that expects the heartbeat
and pages if it goes missing.

## Data flow

```
grove-prod-odoo   grove-odoo-backup.sh  (03:00 UTC cron, on success only)
grove-prod-blogs  grove-blogs-backup.sh (03:30 UTC cron, on success only)
        │  POST [{"job":"odoo-filestore"|"blogs","stamp":...,"host":...}]
        │  Content-Type: application/json   (basic-auth embedded in the URL)
        ▼
grove-obs  OpenObserve  POST /api/default/backup_heartbeat/_json   (:5080)
        │  → row lands in the `backup_heartbeat` LOGS stream
        ▼
OpenObserve scheduled ABSENCE alert  (openobserve/alerts.json)
        │  count(backup_heartbeat WHERE job=…) < 1 over ~28h, checked hourly
        │  → renders keep-event template → POST to Keep  (keep-destination.json)
        ▼
Keep  routes by context_attributes.severity  (keep/workflows/route-*.yml)
        ▼
Discord  #grove-alerts-critical (@here) / #grove-alerts-warning
```

Everything downstream of the OpenObserve alert (destination → Keep → Discord) is
the **same path every other Grove alert already uses** — no new routing.

## The two alerts

| Alert | Job | Severity | Fires when |
|---|---|---|---|
| `odoo-filestore-backup-stale-critical` | `odoo-filestore` | critical | no heartbeat in ~28h |
| `blogs-backup-stale-warning` | `blogs` | warning | no heartbeat in ~28h |

- **One job per backup.** A single shared check would let a green blogs heartbeat
  mask a dead Odoo backup — the exact failure the separate `var.*_ping_url`
  design in `variables.tf` was built to prevent.
- **Filestore is critical, blogs is warning.** The Odoo filestore Spaces mirror is
  the *only* durable off-box copy of customer product photos + `ir.attachment`
  binaries. Blog content is lower-stakes and its MySQL also lives on the durable
  volume; a missed blogs backup needs attention within a day, not a 2am page.
- **~28h window (`period: 1680`), hourly check, 24h silence.** The window is
  comfortably longer than the 24h cron interval plus slack, so a backup that merely
  runs a little late does not page (and it satisfies the board's stated ~26–28h
  target). `silence: 1440` means one page per outage, not one every hour — an alert
  that pages hourly trains the responder to swipe it away.
- **Aggregation `count`, not a bare row-match.** Both alerts set
  `query_condition.aggregation = {function: count}` with `operator '<' threshold 1`.
  An aggregation alert always evaluates its trigger against a numeric result — an
  empty result set yields `count = 0`, and `0 < 1` fires. A plain (non-aggregation)
  row-match risks *green-on-dead*: if the search returns zero rows some evaluators
  never run the trigger, so a dead backup never pages. Aggregation is the shape that
  actually detects **absence**.

## Wiring / config

- **Send side:** `cloud-init-odoo.yaml.tpl` / `cloud-init-blogs.yaml.tpl` — the
  heartbeat is a `curl -X POST --data '[{…}]'` (a bare GET creates no row for the
  absence alert to count). The endpoint is `var.odoo_backup_healthchecks_ping_url`
  / `var.healthchecks_ping_url` (names kept to avoid a churny cross-file rename;
  the *values* are now obs-stack ingest URLs). `""` disables the heartbeat.
- **Secret:** the ingest URL embeds a **scoped, write-only** OpenObserve ingest
  credential and is `op`-injected via `production/.env.op` (never in code). Store
  it in `1P: Goldberry Grove - Admin / Grove Infra / *_backup_heartbeat_ingest_url`.
- **Alerts:** `openobserve/alerts.json`, pushed by `scripts/setup-monitoring.py`.

## Dependency — GOL-381 (prod → obs ingress)

The heartbeat can only land once prod can reach `grove-obs:5080`:
`ingest_source_cidrs` on the obs firewall must allow the prod droplets' egress
IP, and the ingest endpoint must be reachable. That is **GOL-381** (prod
observability wiring). Design and codification (this doc + the alerts + the
script/var changes) proceed in parallel, but the live green heartbeat cannot land
until that ingress exists.

Arming also rebakes each droplet's `user_data` → **forces a droplet REPLACE**
(production-affecting, board-gated). The durable volumes survive a faithful
replace — validated on QA L3 under GOL-825 (filestore manifest byte-identical
across the replace).

## Validation plan (GOL-854 §3 — gated on GOL-381)

1. **Green path.** After arming + apply, trigger a backup (or wait for the cron)
   and confirm a real row lands: query the `backup_heartbeat` stream in
   OpenObserve for `job=odoo-filestore` / `job=blogs` with a fresh `stamp`.
2. **Miss path.** Simulate a miss (disable the cron / block the heartbeat for the
   window, or temporarily shrink the alert `period` for the test) and confirm the
   absence alert fires and pages Discord via Keep.

## Known risk + fallback

Absence detection relies on the scheduled alert evaluating its `< 1` trigger
**even when the search returns zero rows**. Both alerts use an **aggregation
`count`** condition precisely for this: an aggregation always yields a numeric
result, so an empty window evaluates to `count = 0` and `0 < 1` fires. This is
the standard OpenObserve absence-detection shape and is the reason a bare
row-match was rejected. It remains flagged `PENDING LIVE VALIDATION` against
OO v0.91.1 in `alerts.json` (consistent with that file's convention) — step 2 of
the validation plan above *is* that validation, and must run before this switch
is trusted.

Fallback if OO v0.91.1 still does not evaluate the aggregation trigger on an
empty result set: a **Keep interval workflow** triggered every hour that queries
OpenObserve for the newest `backup_heartbeat` row per job and raises a Keep alert
if the newest is older than 28h. Keep's interval trigger evaluates regardless of
upstream data, sidestepping the empty-result question. This keeps the same
Discord routing.

## Alternative considered — manifest-freshness (not chosen)

A monitor that runs **on grove-obs** and lists the Spaces `filestore/manifest/`
prefix, alerting if the newest manifest is >25h old, would avoid *both* the
droplet replace *and* the GOL-381 prod→obs ingress dependency (grove-obs → Spaces,
not prod → grove-obs), and it watches the actual backup *artifact* rather than
"the script reached its last line." It deviates from the board's stated
deliverable (repoint the ping var), so it is recorded here as an option for the
board / Engineering to weigh, not adopted unilaterally.
