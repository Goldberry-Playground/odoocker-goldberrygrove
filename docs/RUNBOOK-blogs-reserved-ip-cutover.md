# Runbook: blogs droplet reserved IP + the pending replace (GOL-387)

**Status:** Phase 1 is ready to run and needs a CEO go. Phase 2 needs a chosen window.
**Applies to:** `infra/terraform/environments/production`, `digitalocean_droplet.blogs`
(`grove-prod-blogs`, id `582968733`, currently `164.90.129.34`) — **LIVE**, serves all
four brand blogs.

Run everything from `infra/terraform/environments/production`, wrapped in
`op run --env-file=.env.op --`. Terraform **>= 1.10** (`versions.tf`); the `terraform`
on `PATH` may be 1.9.8, which refuses to init this config.

---

## Why this exists

`cloudflare_record.blog[*]` used to read `digitalocean_droplet.blogs.ipv4_address`, so a
droplet replace dragged all four `blog.*` A records to `(known after apply)` — a Terraform
apply plus DNS propagation in the middle of an outage. This is not theoretical: a plan
against real prod state today shows the replace already pending.

## The pending replace has TWO triggers, not one

Both are `ForceNew` and both differ from what is applied. From
`terraform show -json`, `replace_paths: [["monitoring"], ["user_data"]]`:

| attribute    | applied (live)                             | rendered by today's code                   |
| ------------ | ------------------------------------------ | ------------------------------------------ |
| `user_data`  | `f6071c899edb6edfac10116029348f0715887c56` | `0baae2b47006d33c72b1ed7858147bfa8495ce4f` |
| `monitoring` | `false`                                    | `true`                                     |

Two things follow, and they matter more than the ticket that opened this:

1. **The `user_data` drift is not recoverable from Terraform.** The DO provider stores
   `user_data` in state as a **SHA1**, not as content — so state cannot tell us what is
   on the live box. The droplet was created **2026-07-07**; the whole production env
   first landed in git on **2026-07-12** (#207). The live droplet was applied from a
   working tree that was never committed as-is. Do not trust a reconstruction; treat the
   replace as the way we find out, and keep the rollback below within reach.

2. **The `monitoring` trigger means prod alerting is currently fiction.** GOL-381 (#256,
   merged 2026-07-15) flipped `monitoring = false -> true` and added four droplet alerts
   (CPU/memory/disk/load5). The **alerts were applied; the flag was not** (it needs this
   replace). Verified against the live DO API: `monitoring` is off, so the `do-agent` is
   not installed and `v1/insights/droplet/*` has no metric source. #256's own comment
   says it: *"with the agent absent those alerts never fire and report green forever,
   which is worse than having no alert at all."* That is the state prod is in right now.
   **Phase 2 is what makes GOL-381's alerting real** — it is not just an SMTP delivery.

## Sequencing is not optional

Reserved IP **first**, replace **second**. Doing the replace first eats the outage this
runbook exists to prevent.

---

## Phase 1 — reserved IP under the running droplet (no replace, no downtime)

Phase 1 never touches the droplet. Proven, not assumed — see "Verification" below.

### 1a. Allocate the reserved IP

```
op run --env-file=.env.op -- terraform apply -target=digitalocean_reserved_ip.blogs
```

`digitalocean_reserved_ip.blogs` has **no** dependency on the droplet, so this is a clean
`Plan: 1 to add, 0 to change, 0 to destroy`. Record the address as `$RIP`.

### 1b. Assign it to the RUNNING droplet, out-of-band

```
doctl compute reserved-ip-action assign $RIP 582968733 --access-token "$TF_VAR_do_token"
```

**Why not Terraform?** `-target` drags in the target's dependencies, and
`digitalocean_reserved_ip_assignment.blogs` depends on the droplet — which has a pending
replace. Verified: `-target=digitalocean_reserved_ip_assignment.blogs` plans
`digitalocean_droplet.blogs must be replaced / Plan: 3 to add, 1 to destroy`. Targeting the
assignment would eat the exact outage we are preventing. Hence: assign out-of-band, import.

Assigning a reserved IP to a live droplet is additive — the droplet keeps answering on
`164.90.129.34` and starts also answering on `$RIP`. Nothing is interrupted. The DO
firewall is per-droplet, not per-address, so `grove-prod-blogs-fw` already covers it.

### 1c. Import the assignment so state matches reality

```
op run --env-file=.env.op -- terraform import digitalocean_reserved_ip_assignment.blogs $RIP,582968733
```

Now a no-op in state, so it never needs targeting again.

### 1d. Repoint the four blog.* records at the reserved IP

```
op run --env-file=.env.op -- terraform apply -target='cloudflare_record.blog'
```

`Plan: 4 to change, 0 to destroy` — the droplet is **not** in this plan. The records are
CF-proxied, so the origin address is never user-visible; this only changes where
Cloudflare's edge sends traffic, from the ephemeral address to the reserved one. Both
route to the same box, so there is no cutover moment.

### 1e. Verify before stopping

- All four blogs serve 200 through Cloudflare:
  `for z in gatheringatthegrove.com goldberrygrove.farm woodworkingeorge.com atthegrovenursery.com; do curl -sS -o /dev/null -w "%{http_code} blog.$z\n" https://blog.$z/; done`
- Origin reachable on the new address:
  `curl -sS -o /dev/null -w "%{http_code}\n" --resolve blog.gatheringatthegrove.com:443:$RIP https://blog.gatheringatthegrove.com/`
- A full `terraform plan` still shows the blogs replace pending and **no** DNS change —
  that pending replace is now safe to defer.

**Rollback:** re-point the records at `164.90.129.34` (`terraform apply -target='cloudflare_record.blog'`
with the value reverted), then `doctl compute reserved-ip-action unassign $RIP`. The
ephemeral address is untouched throughout Phase 1, so rollback is always available.

---

## Phase 2 — the deliberate replace (chosen window)

**Do not start Phase 2 until Phase 1e is green.** Expect a real outage on all four blogs
for the length of a boot: cloud-init + docker install + image pulls + MySQL and 4x Ghost
start. Budget **10–20 min**; the 15m `create` timeout in `blogs.tf` is the ceiling.
DNS does not change, so there is no propagation tail.

### Before the window

- Confirm last night's backup is good — `daily/` in `grove-blogs-backups` has a fresh
  object. The replace reuses the volume, but the volume is the thing at risk.
- Snapshot the droplet for a fast rollback:
  `doctl compute droplet-action snapshot 582968733 --snapshot-name blogs-pre-gol387 --wait`
- Announce the window.

### Run it

```
op run --env-file=.env.op -- terraform apply \
  -target=digitalocean_droplet.blogs \
  -target=digitalocean_volume_attachment.blogs_data \
  -target=digitalocean_reserved_ip_assignment.blogs
```

Terraform destroys the old droplet, creates the replacement, re-attaches the volume, and
**re-points** the reserved IP at the new droplet. `cloudflare_record.blog[*]` is not in
the plan and must not appear in it — if it does, **stop**: Phase 1d did not land.

### The volume re-attaches by device path, not by label

Worth knowing precisely, because a wrong assumption here loses every blog post. GOL-387's
ticket says "confirm `blogs_data` re-attaches by `LABEL=blogsdata`" — it does **not**.
`cloud-init-blogs.yaml.tpl` mounts by DO's stable device path:

```
DEV=/dev/disk/by-id/scsi-0DO_Volume_${volume_name}      # volume_name is stable across replaces
for i in $(seq 1 60); do [ -b "$DEV" ] && break; sleep 5; done   # waits 300s for the attach
if ! blkid "$DEV" >/dev/null 2>&1; then mkfs.ext4 -L blogsdata "$DEV"; fi   # blkid-guarded
```

`LABEL=blogsdata` is only ever set by that `mkfs`, which is guarded by `blkid` — an
existing filesystem is **never** reformatted. The 300s wait absorbs the
droplet-boots-before-TF-attaches race. `digitalocean_volume.blogs_data` carries
`prevent_destroy` and is not replaced; only the attachment is. MySQL secrets are restored
from `/mnt/blogs-data/.grove-mysql-secrets`, which is why a replacement droplet can open
the existing data dir.

### After

- All four blogs 200 (command in 1e). Measure and record the actual window.
- `terraform plan` is clean for `digitalocean_droplet.blogs` — no pending replace.
- `doctl compute droplet get 582968733` → the new droplet reports `monitoring: true`.
- **Confirm GOL-381's alerts now have a metric source** — the agent is installed, so the
  four droplet alerts stop being green-by-default.
- `df -h /mnt/blogs-data` on the new box shows the data volume mounted with content.
- Delete the pre-replace snapshot once the blogs are confirmed healthy.

**Rollback:** restore the droplet from the `blogs-pre-gol387` snapshot and
`doctl compute reserved-ip-action assign $RIP <restored-droplet-id>`. Because DNS points at
the reserved IP, rollback is an IP reassignment — seconds, no DNS change, no propagation.
That property is the whole reason Phase 1 comes first.

---

## Verification (already done, on real prod state)

Read-only, run against the live state backend on 2026-07-15:

- `terraform graph` — **before:** `cloudflare_record.blog -> digitalocean_droplet.blogs`.
  **after:** `cloudflare_record.blog -> digitalocean_reserved_ip.blogs`, with no edge to
  the droplet. `digitalocean_reserved_ip.blogs` has no outgoing edges at all.
- `plan -target=digitalocean_reserved_ip.blogs` → `1 to add, 0 to change, 0 to destroy`.
- `plan -target='cloudflare_record.blog'` → `1 to add, 4 to change, 0 to destroy`, droplet
  absent.
- `plan -target=digitalocean_reserved_ip_assignment.blogs` → droplet **replaced** (this is
  the trap 1b/1c exist to route around).
- `doctl compute reserved-ip list` → empty; no reserved IP exists on the account yet, so
  1a starts from zero.
- `terraform fmt -check` / `validate` clean.
