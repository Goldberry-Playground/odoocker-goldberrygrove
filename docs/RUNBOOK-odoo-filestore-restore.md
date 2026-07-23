# Runbook — Odoo filestore backup & restore (prod)

Covers the nightly filestore backup (GOL-99) and the restore it exists for
(GOL-382). Companion to the day-2 "immutable droplet replace" model locked in
odoocker #242 — that model assumes **reserved IP + verified backups**, and this
is the backups half.

**Scope note:** the filestore is only half of Odoo's state. The other half —
every order, customer and inventory row — is in **Managed Postgres**, which has
its own automated daily backups + 7-day PITR on the basic tier (`postgres.tf`).
Neither half restores a working Odoo alone. §4 covers restoring the pair.

---

## 1. What is backed up, and how

| | |
|---|---|
| **Source** | `/mnt/odoo-filestore/filestore` on `grove-prod-odoo` (the durable block volume, bind-mounted to `/var/lib/odoo` in the container) |
| **Destination** | `spaces:grove-odoo-backups/filestore/…` (`nyc3`, private) |
| **Schedule** | `03:00 UTC` daily — `/etc/cron.d/grove-odoo-backup` |
| **Script** | `/usr/local/bin/grove-odoo-backup.sh` (written by cloud-init) |
| **Log** | `/var/log/grove-odoo-backup.log` |
| **Monitoring** | Healthchecks.io dead-man's switch, `var.odoo_backup_healthchecks_ping_url`. Pings **only on success** |

Bucket layout:

```
filestore/current/            live mirror (rclone sync) — no expiry
filestore/archive/<stamp>/    whatever that run deleted or overwrote — 35d
filestore/manifest/<stamp>.json   {"stamp":…,"files":N,"bytes":N} — no expiry
```

### Why a mirror, not a nightly tarball

Odoo's filestore is **content-addressed**: `filestore/<db>/<2-char>/<sha1-of-content>`.
Files are written once and never modified; only unreferenced ones are GC'd.
That single property drives the whole design:

- **Incremental is free.** A sync uploads only genuinely new attachments. A
  nightly tar of a filestore with a 50 GiB ceiling would re-upload everything,
  every night, and keep 35 copies.
- **A mirror's usual weakness is covered.** `sync`'s only destructive act is
  deleting. `--backup-dir` diverts every deletion into `archive/<stamp>/`
  instead of propagating it, so a bad night is recoverable rather than
  replicated.
- **Cross-time restores are safe** (see §4).

### The guards, and what each is for

- **`mountpoint -q` check — the load-bearing one.** If the block volume fails to
  mount, `/mnt/odoo-filestore` is an empty directory on the root disk. Syncing
  *that* would empty the mirror. The script refuses to run.
- **`--max-delete 1000`** — tripwire. Odoo's attachment GC deletes a trickle; a
  mass deletion means something is wrong, and the run should fail loudly rather
  than quietly mirror it. `archive/` still holds the objects either way.
- **Ping last, on success only.** `set -euo pipefail` means any failure skips the
  ping and trips the dead-man's switch.

---

## 2. Routine verification (do this monthly, ~1 min)

Proves the mirror still matches reality. Read-only, safe on a live prod box.

```bash
ssh root@<odoo-droplet-ip>          # NOTE: the DROPLET ip, not the reserved ip
/usr/local/bin/grove-odoo-restore.sh --verify
```

`rclone check` compares both directions and exits non-zero on any difference.
Expected tail: `[ok] mirror matches the live filestore`.

Also confirm the Healthchecks check for `grove-odoo-backup` is green — the
verify above proves the data is good, the check proves it is still *current*.

---

## 3. Restore the filestore (incident)

**When:** filestore lost/corrupted, volume failure, or a fresh droplet came up
against an empty volume.

```bash
ssh root@<odoo-droplet-ip>
/usr/local/bin/grove-odoo-restore.sh
```

The script stops Odoo (restoring underneath a running server races the
attachment writer), `rclone copy`s `filestore/current` back onto the volume,
fixes ownership, and restarts Odoo.

> **Ownership is not incidental.** The container's `odoo` user is **`100:101`**
> on `odoo:19`, *not* `101:101`. Get this wrong and every attachment read 500s.
> The script resolves it from the image rather than hardcoding — the same bug
> bit GOL-93 and GOL-105. See CLAUDE.md → deploy invariants.

To recover a file that was deleted rather than lost, pull from the archive
instead — `filestore/current` will not have it:

```bash
rclone lsf spaces:grove-odoo-backups/filestore/archive/
rclone copy spaces:grove-odoo-backups/filestore/archive/<stamp> /mnt/odoo-filestore/filestore
```

---

## 4. Full restore: DB **and** filestore

The two halves have independent backup mechanisms and independent restore
points, so they will essentially never land on the same instant.

**This is safe, and content-addressing is why.** Filenames are hashes of
content, so a filestore from time **T1** paired with a DB from a later time
**T2** can only ever be *missing* files (attachments created between T1 and T2)
— it can never serve *wrong bytes* for a hash. The failure mode is a broken
image thumbnail, not silent data corruption.

So restore in this order:

1. **Filestore first**, per §3 — it is the older, coarser half.
2. **Managed PG** to the closest PITR point, via DO console or
   `doctl databases backups`. Prefer a restore point **at or before** the
   filestore's manifest stamp: a DB *older* than the filestore is strictly
   safer, since extra unreferenced files are inert whereas missing ones 404.
3. Reconcile: `ir.attachment` rows whose `store_fname` is absent from the
   filestore surface as broken images. Find them with:

   ```sql
   SELECT id, name, store_fname FROM ir_attachment
   WHERE store_fname IS NOT NULL ORDER BY create_date DESC LIMIT 50;
   ```

   Spot-check the newest few against the filestore. Anything missing was created
   after the filestore's restore point and must be re-uploaded by hand.

---

## 5. Rehearsal: droplet replace + restore-from-scratch

**Do this in QA, never first in prod.** This is the exercise that turns
"we have backups" into "we have verified backups" — GOL-382 acceptance.

1. Note `terraform output odoo_reserved_ip` and the current A record value.
2. Force a replace: `terraform taint digitalocean_droplet.odoo` (or change
   `user_data`), then `terraform apply`. **Start a timer.**
3. Watch: the droplet is destroyed and recreated, the volume re-attaches by
   `LABEL=filestore`, and `digitalocean_reserved_ip_assignment.odoo` re-points
   the reserved IP at the new droplet.
4. **Assert `cloudflare_record.odoo` shows NO diff.** This is the whole point.
   If DNS changed, the reserved IP is not wired correctly and the day-2 model is
   still fiction.
5. Stop the timer when `https://odoo.<zone>/` serves 200 again. **Record the
   window** — #242 budgets ~10 min; the measured number is what we actually
   commit to.
6. Restore-from-scratch: wipe `/mnt/odoo-filestore/filestore` on the QA box, run
   `grove-odoo-restore.sh`, then `--verify`, then load a product image in the UI.

> **The replace is destroy-then-create, inherently.** `create_before_destroy`
> cannot help here: a DO block volume attaches to exactly one droplet at a time,
> so the new droplet cannot mount the filestore until the old one releases it.
> That constraint — not DNS — is what sets the floor on the maintenance window.
> The reserved IP's job is to make sure DNS propagation is not *added* on top of
> it.

---

## 6. Related

- `docs/RUNBOOK-db-promotion-cutover.md` — the **planned promotion** companion
  (freeze → promote → verify between environments) + the fail-loud attachment
  invariant. This runbook is DR restore; that one is env-to-env cutover.
- `infra/terraform/environments/production/odoo.tf` — volume, backups bucket, scoped key, reserved IP
- `infra/terraform/environments/production/cloud-init-odoo.yaml.tpl` — backup + restore scripts
- odoocker **#242** — day-2 model this underpins
- **GOL-99** — nightly filestore backup · **GOL-382** — reserved IP + verified backups
