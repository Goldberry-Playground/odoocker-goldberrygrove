# Runbook — QA → prod data promotion (freeze → dump+filestore → restore → reconfig → verify)

**Owner:** DevOps - Terra · **Ticket:** GOL-1329 (parent GOL-1323) · **Refs:**
GOL-484 (prod `grove_headless` install — gates the *real* cutover), vault
`Grove Production Launch` → checklist item *"Data promotion rehearsed once"*.

> **What this is.** QA Odoo (`qa.gatheringatthegrove.com`) has been the Grove
> **system of record for REAL customer orders/sales since 2026-07-09.** Prod must
> be brought up from *that* data, not from a blank re-seed. This runbook is the
> rehearsed, self-verifying path that promotes QA's live data to prod safely, and
> the **hard block** that stops anyone from re-seeding QA out from under it.
>
> **What this is NOT.** This is the runbook + **one scratch rehearsal**. The real
> prod cutover weekend is a *separate* launch step: it runs **after GOL-484**
> (prod `grove_headless` installed) and **before** the hub/goldberry apex window
> closes the launch (GOL-287 / GOL-1279).

This runbook composes existing, hardened tooling — it does not replace it:

| Concern | Tool | Doc |
|---|---|---|
| Move DB **with** the filestore, fail-loud on attachment gap | `scripts/promote-db.sh` + `scripts/check-attachment-invariant.sh` | `docs/RUNBOOK-db-promotion-cutover.md` |
| Strip test data before the freeze | `scripts/qa-test-data-cleanup.sh` | `docs/RUNBOOK-qa-test-data-cleanup.md` |
| Rewrite env-specific config on the restored DB | `scripts/prod-reconfig.py` | *this doc, §5* |
| Order/invoice/tax integrity gates | `scripts/promotion-integrity-gates.py` | *this doc, §6* |
| Stop a blind QA re-seed | `scripts/qa-reseed-guard.py` | *this doc, §8* |

---

## 0. Preconditions & credentials

- **GO decision** recorded (CEO) with the freeze window scheduled. A freeze stops
  QA order-taking — coordinate the window; do not freeze during market hours.
- **Prod `grove_headless` installed** (GOL-484) — required for the *real* cutover
  only. The scratch rehearsal (§9) does **not** need it.
- Credentials via 1Password `op run` (least privilege):
  - QA Odoo XML-RPC user with read + the delete rights `qa-test-data-cleanup`
    needs (reuse the canary user — see `scripts/qa-test-data-cleanup.env.op`).
  - Prod/scratch Odoo XML-RPC user with settings write for `prod-reconfig.py`.
  - `op` refs for every secret the reconfig spec pulls (`PROD_WEB_BASE_URL`,
    `PROD_SMTP_*`, Stripe live keys, webhook secrets) — see
    `scripts/prod-reconfig.env.op`.
- SSH to the QA and prod L3 droplets (`grove_qa_admin` key + self-serve firewall
  recipe in the deploy-test skill). Postgres runs **inside** the `postgres`
  container; the durable filestore is the block volume bind-mounted at
  `/mnt/odoo-filestore` (**not** the ephemeral root disk — GOL-99).

**Announce the freeze** in the Discord ops channel at start and thaw at end
(`scripts/discord-status.sh`). The freeze/thaw signal is the human half of the
`--freeze-cmd`/`--unfreeze-cmd` the bundler runs.

---

## 1. Step 0 — test-order hygiene (BEFORE the freeze)

QA accumulates synthetic-canary orders, checkout-journey orders, and the
`SYNTHETIC-CANARY` product over a release cycle. Promote **clean** data — but the
teardown is *surgical*: it only removes records at RFC-reserved test domains
(`*.invalid`, `example.com`…) that a real customer can never own. It never keys on
dates/amounts/state. Full rationale: `docs/RUNBOOK-qa-test-data-cleanup.md`.

```bash
# dry-run first (deletes nothing) — eyeball the plan:
QA_CLEANUP_ENV_OP=scripts/qa-test-data-cleanup.env.op \
  bash scripts/qa-test-data-cleanup.sh --json
# then apply:
QA_CLEANUP_ENV_OP=scripts/qa-test-data-cleanup.env.op \
  bash scripts/qa-test-data-cleanup.sh --apply --json
```

Do this **before** the freeze so the frozen snapshot is already clean and the
`promote-db.sh` manifest counts reflect real data only.

---

## 2. Capture the source integrity baseline (BEFORE the freeze)

Record what QA holds so the target can be checked against it (§6, gate 4):

```bash
op run --env-file=scripts/prod-reconfig.env.op -- \
  python3 scripts/promotion-integrity-gates.py --emit-baseline > /tmp/promo-baseline.json
cat /tmp/promo-baseline.json    # {sale_orders, confirmed_orders, partners, posted_invoices}
```

Keep `/tmp/promo-baseline.json` — it is evidence and the input to the
completeness gate on the target side.

---

## 3. FREEZE — stop writes on QA

Dumping the DB and taring the filestore are not atomic; an attachment written
between them lands in the dump but not the tar (or vice-versa) → a false (or
real, tolerance-swallowed) invariant gap. Freeze first. The bundler takes the
freeze/unfreeze commands so the freeze is **recorded and always undone**:

- Preferred: stop the QA Odoo container (`docker compose stop odoo`). Postgres
  stays up for the dump.
- Post the freeze to Discord ops.

(The `--freeze-cmd`/`--unfreeze-cmd` below run this for you.)

---

## 4. PROMOTE — bundle DB + filestore together

Run against the **QA** droplet. The filestore moves *in the same bundle* as the
`pg_dump` — this is the structural fix for the 2026-07-23 asset outage
(`docs/RUNBOOK-db-promotion-cutover.md` §1). Capture the dump sha256 for evidence.

```bash
# ON the QA droplet
PG_DUMP="docker compose exec -T postgres pg_dump -U odoo" \
PSQL="docker compose exec -T postgres psql -U odoo" \
scripts/promote-db.sh bundle \
  --db odoo \
  --filestore /mnt/odoo-filestore/filestore/odoo \
  --out ./promote-bundle \
  --freeze-cmd 'docker compose stop odoo' \
  --unfreeze-cmd 'docker compose start odoo'

sha256sum promote-bundle/db.sql.zst promote-bundle/filestore.tar.zst | tee promote-bundle/SHA256SUMS
cat promote-bundle/manifest.json    # source-side attachment invariant baseline
```

> QA and prod both run a DB literally named `odoo`; the `--filestore` path is the
> block-volume host path, **not** the container-only `/var/lib/odoo`. Confirm with
> `lsblk` / DO Volumes which host dir is the durable mount (GOL-99).

Copy `promote-bundle/` to the **prod** (or scratch) droplet (scp/rsync over the
admin SSH path). Re-verify `sha256sum -c SHA256SUMS` on the far side.

---

## 5. RESTORE + RECONFIG on the target

### 5a. Restore (loads DB, extracts filestore, runs the fail-loud invariant)

```bash
# ON the target droplet — target a freshly dropped/created DB (see runbook §3c note)
docker compose exec -T postgres psql -U odoo -c 'DROP DATABASE IF EXISTS odoo;'
docker compose exec -T postgres psql -U odoo -c 'CREATE DATABASE odoo OWNER odoo;'

IMG="ghcr.io/goldberry-playground/grove-odoo:${ODOO_TAG:-latest}"
OWNER="$(docker run --rm --entrypoint id "$IMG" -u odoo):$(docker run --rm --entrypoint id "$IMG" -g odoo)"
PSQL="docker compose exec -T postgres psql -U odoo" \
scripts/promote-db.sh restore \
  --db odoo \
  --filestore /mnt/odoo-filestore/filestore/odoo \
  --in ./promote-bundle \
  --owner "$OWNER"     # 100:101 on odoo:19 — resolve from the image, never hardcode
```

A breached invariant exits non-zero → **do not proceed**; every asset would 500.

### 5b. Idempotent reconfig — rewrite env-specific config, then ASSERT

The restored DB carries **QA's** `web.base.url`, Stripe *test* providers, QA
Mailgun server, and crons left disabled by the freeze. `prod-reconfig.py` rewrites
them from a committed, `${ENV}`-referencing spec and **asserts every post-condition**
(it does not assume the writes worked). Secret values come from `op`, never the spec.

```bash
# FIRST (rehearsal only): dump what the restored DB currently holds, so you can
# confirm the spec's field names/domains match the live schema:
python3 scripts/prod-reconfig.py --target scratch \
  --spec scripts/prod-reconfig.spec.example.json --report --json | tee /tmp/reconfig-before.json

# APPLY (idempotent) — writes, then runs all assertions:
op run --env-file=scripts/prod-reconfig.env.op -- \
  python3 scripts/prod-reconfig.py --target scratch \
    --spec scripts/prod-reconfig.spec.example.json --apply --json | tee /tmp/reconfig-apply.json
```

- For the **real prod** run, use `--target prod` and set `PROD_RECONFIG_CONFIRM=yes`
  (a deliberate, auditable opt-in — mirrors "never bare-apply prod").
- The spec (`scripts/prod-reconfig.spec.example.json`) covers: `web.base.url`
  (+ `.freeze`), `report.url`, re-enable disabled crons, Stripe test→live/enabled,
  Mailgun prod SMTP, and asserts **no config value still points at the QA host**.
  Finalise it against `--report` output before the real cutover.
- Re-run any time with `--check` (assertions only, no writes) to re-verify.
- **Publish/Stripe webhook secrets** that are container-env-injected (per
  `GOL-1004`/`GOL-1016`) are set by the prod compose `environment:` block, not the
  DB — confirm they carry prod values in the running container (`docker compose
  exec odoo env | grep -E 'STRIPE|PUBLISH_WEBHOOK'`) as part of 5b.

---

## 6. Integrity gates — the go/no-go on the data itself

"The restore ran" ≠ "the data is safe to sell against." Run the read-only gates
on the target; a hard-gate failure exits non-zero → **do not thaw / cut over**.

```bash
op run --env-file=scripts/prod-reconfig.env.op -- \
  python3 scripts/promotion-integrity-gates.py \
    --baseline /tmp/promo-baseline.json --json | tee /tmp/integrity.json
```

Gates:
1. **order-sequence continuity** — `sale.order` sequence next-number > highest
   existing order, so the next real order can't collide with a promoted one.
2. **invoice-numbering continuity** — same for posted customer invoices
   (`account.move`, legally sequential — a collision/gap is an accounting defect).
3. **WV-nexus tax spot-check** — WV-ship orders carry tax; non-WV don't (Grove has
   nexus in WV only, GOL-1021). Catches tax config that didn't survive the move.
4. **promotion completeness** — target counts ≥ the §2 source baseline (no rows
   lost in transit).

---

## 7. Smoke checkout + thaw

- Bring the target stack up (if not already) and load the storefront.
- Run a **real end-to-end checkout** (add to cart → checkout → payment → order
  confirmation email). On the scratch rehearsal, a Stripe *test* card is fine and
  is the evidence; on prod, do a canary live order and refund it.
- Confirm attachment/asset serving (no unstyled site — the 2026-07-23 signature).
- **Thaw**: `docker compose start odoo` on QA (the bundler's `--unfreeze-cmd`
  already did this on exit; confirm it) and post thaw to Discord ops.

---

## 8. QA-reseed HARD BLOCK (enforced)

Once prod is verified, prod becomes the system of record and QA may be reseeded —
**but not before**. A blind reseed before a verified promotion destroys real
revenue data. The block is a machine-checkable marker on the QA DB.

**Arm the marker only after prod is verified** (final promotion step):

```bash
op run --env-file=scripts/qa-test-data-cleanup.env.op -- \
  python3 scripts/qa-reseed-guard.py set \
    --dump-sha256 "$(awk '/db.sql.zst/{print $1}' promote-bundle/SHA256SUMS)" \
    --confirmed-by "rick" \
    --verified-at "<ISO-timestamp-of-prod-verification>" \
    --note "GOL-1329 promotion verified"
```

**Every reseed / DB re-init MUST gate on it** and abort on non-zero:

```bash
python3 scripts/qa-reseed-guard.py check || {
  echo "QA reseed BLOCKED — promote+verify to prod first (GOL-1329)"; exit 1; }
```

`check` exits `3` (BLOCKED) when the marker is absent, `0` when present. There is
no standalone QA-reseed script in this repo today; this guard is the required
precondition for any future one, and is documented here as **step 0 of any reseed**.
Re-arm the block for a new cycle with `qa-reseed-guard.py clear --i-understand`.

---

## 9. The one scratch rehearsal (evidence)

The rehearsal proves the whole chain end-to-end against a **scratch restore
(NOT prod)** and captures evidence. Two viable scratch targets:

- **Ephemeral scratch droplet** (preferred — exercises the real restore path;
  same recipe as the GOL-1328 blogs restore test): provision a throwaway droplet,
  install the grove-odoo compose, restore the bundle, run §5b/§6/§7, then destroy
  it. Costs nothing after teardown.
- **Scratch DB on the QA box**: restore the bundle into a `promo_rehearsal` DB on
  the QA droplet's Postgres; run reconfig/gates against it via an Odoo shell/second
  service; drop the DB after. Cheaper, but a second HTTP-serving Odoo is needed for
  the smoke checkout.

**Evidence to capture** (attach to GOL-1329):
- `promote-bundle/SHA256SUMS` (dump + filestore sha256) and `manifest.json`.
- `/tmp/promo-baseline.json` (source counts) vs the target `--json` gate output
  (post counts) — row/order counts pre vs post.
- `/tmp/reconfig-apply.json` — all reconfig assertions PASS.
- `/tmp/integrity.json` — all integrity gates PASS.
- A screenshot / order id from the smoke checkout on the scratch instance.

---

## 10. Rollback

Every leg is reversible; nothing here is a one-way door **except** the real DNS
apex flip (out of scope — GOL-287/1279, instant DNS rollback of its own).

| If it fails at… | Rollback |
|---|---|
| Restore invariant (§5a) | Nothing served yet. Fix the filestore/bundle, re-drop the target DB, re-restore. QA is frozen but intact — thaw it and reschedule. |
| Reconfig assertions (§5b) | `prod-reconfig.py` only rewrote config on the *target*; re-run `--report`, fix the spec/env, re-`--apply` (idempotent). Target not yet serving traffic. |
| Integrity gates (§6) | Do **not** thaw/cut over. The target is discardable; QA (frozen, unchanged) remains the system of record. Investigate, re-bundle, retry. |
| After thaw, defect found | QA was never destroyed by this process — it was only frozen and copied. Point traffic back at QA (it holds the authoritative data); the marker (§8) was NOT armed, so QA reseed stays blocked. |

**Invariant that makes rollback safe:** this process only ever *reads* QA (dump +
filestore copy) and *writes* the target. QA is never mutated beyond freeze/thaw,
so "roll back" always means "keep using QA." The reseed marker is armed **last**,
only after prod is verified — so a mid-promotion abort can never unlock a
destructive QA reseed.
```
