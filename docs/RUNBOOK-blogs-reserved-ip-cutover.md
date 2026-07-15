# Runbook: blogs droplet reserved IP + the pending replace (GOL-387)

**Status:** **Phase 1 is DONE — applied to prod 2026-07-15, zero downtime.** Phase 2 is
NOT scheduled and still needs an explicitly named window (see "Phase 2 is deliberately
held" below).
**Applies to:** `infra/terraform/environments/production`, `digitalocean_droplet.blogs`
(`grove-prod-blogs`, id `582968733`, ephemeral `164.90.129.34`) — **LIVE**, serves all
four brand blogs.

## Phase 1 outcome (2026-07-15)

`digitalocean_reserved_ip.blogs` = **`159.89.243.121`**, assigned to the running droplet
`582968733` (created `2026-07-07` — **not** replaced; verified via the DO API). All four
`cloudflare_record.blog[*]` now point at the reserved IP.

Verified after the cutover, cache-busted so Cloudflare had to fetch the origin
(`cf-cache-status: MISS` — a plain request returns a cached `HIT` and proves nothing):

| host                          | before | after            |
| ----------------------------- | ------ | ---------------- |
| `blog.woodworkingeorge.com`   | 200    | **200** (`MISS`) |
| `blog.atthegrovenursery.com`  | 200    | **200** (`MISS`) |
| `blog.gatheringatthegrove.com`| 404    | **404** (`MISS`) |
| `blog.goldberrygrove.farm`    | 404    | **404** (`MISS`) |

(The two 404s are the expected pre-cutover state, unchanged by this work.)

**The acceptance criterion, proven:** a full plan against real prod state now shows the
droplet replace still pending but **zero `cloudflare_record.blog` entries in the change
set**. The replace no longer moves DNS. `digitalocean_reserved_ip_assignment.blogs` does
show `must be replaced` — that is correct and intended: it re-points **the same**
`159.89.243.121` at the new droplet (`ip_address` unchanged).

**Origin reachability could not be pre-tested from the agent sandbox** — direct curl to
the origin returns 000 on the reserved *and* the ephemeral IP alike (no direct egress;
the firewall allows 443 from `0.0.0.0/0`, so it is not the cause). It was instead proven
with a **canary**: `blog.gatheringatthegrove.com` (pre-cutover, no uptime check, nothing
depends on it) was repointed first and still returned a cache-busted `404 MISS` — origin
reached through the reserved IP, no 521/522. Only then were the two live blogs moved.
Use that canary order again for any similar cutover.

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

2. **The `monitoring` trigger means half of GOL-381's alerting is currently inert.**
   GOL-381 (#256, merged 2026-07-15) flipped `monitoring = false -> true` and added four
   tag-scoped droplet alerts (CPU/memory/disk/load5). The **alerts were applied; the flag
   was not** (it needs this replace). Verified against the live DO API: `monitoring` is
   off, so the `do-agent` is not installed and `v1/insights/droplet/*` has no metric
   source. #256's own comment says it: *"with the agent absent those alerts never fire and
   report green forever, which is worse than having no alert at all."*

   Be precise about what this does and does not mean:

   | alert | mechanism | working today? |
   | --- | --- | --- |
   | `monitor_alert.droplet` cpu / memory / disk / load5 | `do-agent` metrics | **NO — inert** |
   | `uptime_alert.down` (`down_global`) blog-ggg, blog-nursery | DO's external probe network | yes |
   | `uptime_alert.ssl_expiry` blog-ggg, blog-nursery | external | yes |

   So a **hard outage** still pages: the droplet serves all four blogs, and two of them
   are externally probed. What is invisible is the **slow burn** — disk filling, memory
   exhaustion, load — which is exactly what the resource alerts exist to catch, on a 60 GB
   box running MySQL plus four Ghost instances. (`blog.gatheringatthegrove.com` and
   `blog.goldberrygrove.farm` are deliberately excluded from uptime checks while they 404
   pre-cutover, so they have no external coverage of their own.)

   **Phase 2 is what makes the resource half of GOL-381's alerting real** — it is not just
   an SMTP delivery.

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

## Phase 2 is deliberately held — do not run it off the Phase 1 approval

Phase 1's approval card (`8fd405a4`) was worded "Accept = Phase 1 now + Phase 2 in a
window you name." It was accepted, but **no window was named**, so Phase 2 has no
approval to stand on. Three further reasons it was not executed:

1. **The acceptance shows signs of a queue-clear, not a considered go.** A stray probe
   card reading literally `"probe"` was accepted one second after this one.
2. **The card's Phase 2 rationale contains a claim that was publicly retracted** — it
   says a blogs outage "pages nobody today". Only the `do-agent`-dependent alerts are
   inert; DO's external uptime probes work, so a hard outage *does* page. The real gap
   is slow-burn (disk/memory/load).
3. **Phase 1 removed the urgency.** DNS is now decoupled, so the pending replace is safe
   to defer indefinitely — which is exactly what Phase 1 was for. The replace's remaining
   risk is *boot/reproducibility* (GOL-385: the live box was applied from never-committed
   code, so nobody knows what comes back up), and that is not de-risked by a window; it
   is de-risked by GOL-388's rehearsal on qa-l3, a box nobody depends on.

Phase 2 therefore belongs to **GOL-385**, sequenced after **GOL-388**'s rehearsal, and
needs a fresh, explicit approval that names a window. The procedure below stays here
because it is the procedure — not because it is scheduled.

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
- **Confirm GOL-381's four resource alerts now have a metric source** — the agent is
  installed, so cpu/memory/disk/load5 stop being green-by-default. (The uptime/ssl alerts
  were already working and are unaffected.)
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
