# Runbook — DB promotion / cutover (move the filestore WITH the DB)

Hardens the **freeze → promote → verify** path so the 2026-07-23 QA asset
outage cannot recur at the prod keystone cutover (GOL-737). Parent: GOL-742 ·
Owner: DevOps - Terra.

**Applies to every DB promotion:** local → QA, QA → prod, and the preview
restore path (which promotes a sanitized prod snapshot into an ephemeral
preview). Companion to `RUNBOOK-odoo-filestore-restore.md` (that one is the
*disaster-recovery* restore; this one is the *planned promotion* between
environments).

---

## 1. The incident this exists to prevent (2026-07-23)

QA was promoted from a local Goldberry Odoo instance by dumping the **DB only**
— the filestore was left behind. Result:

- **676** file-backed `ir_attachment` rows vs **47** files on disk.
- Every attachment-served resource `500`'d: **all** CSS/JS asset bundles (a
  fully unstyled site), logo, favicon, **452** payment-method icons, **35**
  partner images.
- Recovery took a content-hash file restore of **552** files plus clearing
  stale asset-bundle rows.

Nothing failed at promotion time. The miss only surfaced as browser 500s after
cutover. **The same miss at prod cutover 500s the launch.**

The root cause is that "also copy the filestore" was a *runbook footnote you can
forget*. This runbook + `scripts/promote-db.sh` make it a **structural step the
tool performs**, and `scripts/check-attachment-invariant.sh` makes forgetting it
**fail loud** before traffic switches.

---

## 2. Why a straight copy is correct (and safe)

Odoo's filestore is **content-addressed**: an attachment's bytes live at
`filestore/<db>/<2-char>/<sha1-of-content>`, and `ir_attachment.store_fname`
holds the `<2-char>/<sha1>` path. Two consequences drive this whole runbook:

1. **Files are instance-portable.** The path is a hash of the *content*, not of
   any instance identity — so a plain `cp`/`rsync`/`tar` of `filestore/<db>/`
   from source to target is correct. No rewrite, no re-keying.
2. **Many rows dedup onto one file.** The honest "expected files" figure is the
   count of **DISTINCT `store_fname`**, not the raw row count. The invariant
   check compares distinct-store_fname against files-on-disk for exactly this
   reason (676 rows in the incident were only ~552 unique files).

The dangerous direction is **DB-expects-more-than-disk-has** (missing bytes →
the 500s). Extra files on disk are inert orphans — they 404 nothing — so the
check only *warns* on those.

---

## 3. Ordering: freeze → promote → verify (do not reorder)

### 3a. FREEZE — stop writes on the source

Dumping the DB and taring the filestore are not atomic. An attachment written
*between* the two steps lands in the DB dump but not the tar → a false invariant
breach (or, worse, a real gap the tolerance swallows). Freeze first:

- **Preferred:** stop the source Odoo container (`docker compose stop odoo`), or
  scale it to 0. Postgres stays up for the dump.
- **Acceptable:** put the source DB in a state where the app can't write
  (maintenance mode + no background crons).
- Pass the freeze/unfreeze to the bundler so it's recorded and always undone:
  `promote-db.sh bundle … --freeze-cmd 'docker compose stop odoo' --unfreeze-cmd 'docker compose start odoo'`.

### 3b. PROMOTE — bundle DB + filestore together

```bash
# On / against the SOURCE (example: local Goldberry OrbStack stack)
PG_DUMP="docker compose exec -T postgres pg_dump -U odoo" \
PSQL="docker compose exec -T postgres psql -U odoo" \
scripts/promote-db.sh bundle \
  --db goldberry \
  --filestore /var/lib/docker/volumes/grove_odoo_filestore/_data/goldberry \
  --out ./promote-bundle \
  --freeze-cmd 'docker compose stop odoo' \
  --unfreeze-cmd 'docker compose start odoo'
```

Produces `promote-bundle/{db.sql.zst, filestore.tar.zst, manifest.json}`. The
manifest pins the **source-side** distinct-store_fname / row / file counts —
the baseline the target is checked against.

### 3c. VERIFY — restore and let the invariant gate the cutover

```bash
# On / against the TARGET (example: prod or QA)
PSQL="docker compose exec -T postgres psql -U odoo" \
scripts/promote-db.sh restore \
  --db grove \
  --filestore /var/lib/odoo/filestore/grove \
  --in ./promote-bundle \
  --owner 100:101        # see §5 — resolve from the image, don't blind-hardcode
```

The `restore` verb loads the dump, extracts the filestore, and runs the
fail-loud invariant. **A breach exits non-zero and you MUST NOT cut over** — the
filestore did not land and every asset will 500. Fix the filestore, re-run,
*then* bring the stack up / flip DNS.

> **`--db` must be a freshly created (or dropped-and-recreated) database.** The
> dump is a plain `pg_dump` loaded without `--clean`/`--create`; loading it into
> a **populated** DB errors mid-load. That fails loud under `set -e` (safe, no
> silent corruption), but leaves a confusing half-load — so drop/recreate the
> target first: `dropdb --if-exists grove && createdb -O odoo grove`.

Run the invariant standalone at any time (e.g. as the last gate in a manual
cutover):

```bash
PSQL="docker compose exec -T postgres psql -U odoo" \
scripts/check-attachment-invariant.sh --db grove \
  --filestore /var/lib/odoo/filestore/grove --mode fail
```

---

## 4. Consistency footguns — mimetype / checksum / ETag (all three bit us)

A file that is merely *present* is not sufficient. On the 23rd, files restored
under the **wrong** attachment metadata produced failures that looked like
success:

- **mimetype mismatch** → Odoo serves the bytes with the wrong `Content-Type`.
  The browser gets HTTP **200** but the resource is **undecodable** (an image
  served as `text/plain`, a JS bundle served as `text/html`). Green in the
  network tab, broken on the page.
- **checksum mismatch** → `ir_attachment.checksum` (the sha1 Odoo trusts) no
  longer matches the on-disk bytes. Asset-bundle rows whose checksum is stale
  point at bytes that were regenerated → 500 or stale served content.
- **ETag / 304 poisoning** → Odoo derives the `ETag` from the attachment
  checksum. Serve a repaired-but-mismatched file once and the browser caches it;
  subsequent requests get **304 Not Modified** and the client re-uses the
  **poisoned** cached copy. The bad state pins itself into every visitor's cache
  and survives a later fix until the ETag changes.

**Rules that follow:**

1. Prefer a **whole-filestore move** (this runbook) over hand-repairing
   individual files — a straight content-addressed copy can't produce a
   metadata mismatch, because the filename *is* the content hash.
2. If you *do* hand-repair files (DR, not promotion), also **clear stale
   asset-bundle attachment rows** so Odoo regenerates them against the real
   bytes (this is what recovery did on the 23rd). Bundle rows are the
   `ir_attachment` rows for `/web/assets/...`; deleting them is safe — Odoo
   rebuilds on next request.
3. After any repair, **force new ETags**: bump the assets version / restart Odoo
   so regenerated bundles get fresh checksums, and tell testers to hard-reload
   (a plain reload honours the poisoned 304).

---

## 5. Filestore ownership (don't reintroduce a different 500)

The container's `odoo` user is **`uid=100 gid=101`** on the official `odoo:19`
image — **not** 101:101. A filestore owned by the wrong uid makes Odoo unable to
read/write attachments — the same 500 by a different cause (bit GOL-93, GOL-105).
**Resolve from the image, never blind-hardcode:**

```bash
IMG="ghcr.io/goldberry-playground/grove-odoo:${ODOO_TAG:-latest}"
OUID=$(docker run --rm --entrypoint id "$IMG" -u odoo 2>/dev/null || echo 100)
OGID=$(docker run --rm --entrypoint id "$IMG" -g odoo 2>/dev/null || echo 101)
# then: promote-db.sh restore … --owner "$OUID:$OGID"
```

Verify live: `docker exec grove-odoo-1 id odoo`. Full rationale:
`.claude/skills/deploy-test/SKILL.md` → "Cloud-init / droplet invariants" and
CLAUDE.md deploy invariants.

---

## 6. Where the invariant runs automatically

| Path | Enforcement |
|---|---|
| **Manual local→QA / QA→prod** | `promote-db.sh restore` (fail mode) — this runbook §3c. |
| **Prod keystone cutover (GOL-737)** | Run the standalone check as the final go/no-go gate before flipping DNS — §3c snippet. |
| **Preview restore** (prod snapshot → ephemeral preview) | `restore.sh` in `infra/terraform/environments/preview/cloud-init.yaml.tpl` runs the check in **warn** mode: a breach screams in `/var/log/grove-restore.log` and Discord-adjacent triage, but does not brick an ephemeral per-PR droplet on a benign sanitizer delta. Previews self-heal on the next PR push. |

The nightly `scripts/preview/sanitize-and-upload.sh` and
`scripts/preview/seed-snapshot.sh` already tar the filestore alongside the dump
(steps `[2/5]`/`[4/5]`) — this runbook makes the **verify** half loud too.

---

## 7. Related

- `scripts/promote-db.sh` — freeze-aware bundler + restorer.
- `scripts/check-attachment-invariant.sh` — the fail-loud invariant.
- `docs/RUNBOOK-odoo-filestore-restore.md` — DR restore (§4 covers DB+filestore
  restore-point skew; same content-addressing logic).
- ADR 004 (`docs/ADR/004-qa-promotion-model.md`) — the QA promotion model this
  cutover feeds.
- GOL-742 (parent) · GOL-744 (this) · GOL-737 (prod keystone cutover).
