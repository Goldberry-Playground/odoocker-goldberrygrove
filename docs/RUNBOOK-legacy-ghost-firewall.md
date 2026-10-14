# RUNBOOK — fence the legacy Ghost snowflake (GOL-2566)

Holding action for `ghostgoldberrygrove-nyc1` (droplet `468914087`,
`178.128.152.218`, created 2025-01-10), the last of the two Ghost snowflakes named
in `infra/terraform/environments/production/main.tf`. It is in **no** DO cloud
firewall. Code: `infra/terraform/environments/production/legacy-ghost-fw.tf`.

## Why this is not just tidy-up

Verified read-only from the agent plane on 2026-09-29 (nothing applied):

| check | result |
|---|---|
| `:22` | OPEN to `0.0.0.0/0` since 2025-01-10 |
| `:80` | OPEN, 301 → https |
| `:443` | OPEN, Ghost |
| TLS cert | `CN=goldberrygrove.farm`, **expired 2025-10-07** — ~12 months of dead certbot |
| `https://178.128.152.218/ghost/` | **200 — Ghost admin login, on the public internet** |
| `/ghost/api/admin/site/` | 200 |
| DO cloud firewall membership | **none** (`grove-prod-odoo-fw` `[]`, `grove-obs-fw` `[583896515]`, `grove-prod-blogs-fw` `[593114105]`, `General` `[]`) |

An unattended CMS admin login reachable from the internet behind a year-expired
certificate is the real finding here; `:22` is only the part that was noticed first.

## It is DNS-orphaned, so fencing HTTP breaks nothing

Across all four Cloudflare zones — `goldberrygrove.farm`, `atthegrovenursery.com`,
`woodworkingeorge.com`, `gatheringatthegrove.com` — **zero** DNS records resolve
to `178.128.152.218`. Every `blog.*` A record points at `159.89.243.121` (the
`grove-prod-blogs` reserved IP); every apex is a CNAME to an App Platform
frontend. No DO load balancer and no reserved IP reference it. The box is
reachable only by raw IP, so `:80`/`:443` are fenced to `admin_ip_cidrs` by
default alongside `:22`.

## Do NOT destroy this droplet yet

It holds **29 authored posts** (its own `sitemap-posts.xml`) and it is the **sole
copy**. `goldberrygrove.farm/blog` serves a *different*, 4-post set from
`grove-prod-blogs`. A 5-slug sample of the legacy archive all 404s on the live
site:

```
pollarding-vs-coppicing-two-ancient-techniques-powering-modern-agroforestry  404
november-at-goldberry-grove                                                 404
the-peoples-nut-that-fed-appalachia                                         404
mycoforestry-101-trees-fungi-and-the-art-of-growing-together                404
planting-without-a-mask-why-i-started-goldberry-grove                      404
```

The archive was never migrated. **Retirement (GOL-863) is gated on a content
migration**, not on a CEO go alone. There is a snapshot (`241457688`) and DO
backups are on, but a snapshot is disaster recovery, not a migration.

Also correcting GOL-863's premise: the sibling snowflake
`gatheratthegrove-blog-nyc` is already gone from the account, so the standing
cost is **one $32/mo droplet plus ~$6.40/mo DO backups**, not ~$96/mo.

## Apply

Terraform never owns the droplet — it is resolved via a `data` source, so
`terraform destroy` in this env cannot delete it. Only the firewall is managed.
Attaching a DO cloud firewall is an in-place change on the firewall object: the
droplet is not touched, rebooted or replaced.

```bash
cd infra/terraform/environments/production
terraform init -backend-config=backend.hcl
op run --env-file=.env.op -- terraform plan  -var legacy_ghost_firewall_enabled=true
op run --env-file=.env.op -- terraform apply -var legacy_ghost_firewall_enabled=true
```

Expected plan: **1 to add, 0 to change, 0 to destroy** — one
`digitalocean_firewall.legacy_ghost[0]` with `droplet_ids = [468914087]`. If the
plan shows anything being changed or destroyed, **stop**.

To make it the standing default instead of a flag, set
`legacy_ghost_firewall_enabled = true` in the env's tfvars.

## Verify

```bash
# 1. membership — the box is now inside the firewall
curl -sS -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" \
  "https://api.digitalocean.com/v2/firewalls" \
  | python3 -c 'import json,sys; [print(f["name"], f["droplet_ids"]) for f in json.load(sys.stdin)["firewalls"]]'
# expect: grove-legacy-ghost-fw [468914087]

# 2. network — :22 must stop answering from a non-operator address
#    (run from somewhere NOT in admin_ip_cidrs; times out / refuses when correct)
timeout 5 bash -c 'echo >/dev/tcp/178.128.152.218/22' && echo STILL-OPEN || echo FENCED
```

`FENCED` on step 2 from a non-operator address is the success condition. Josh's
own addresses are in `admin_ip_cidrs`, so from his machine `:22` should still
work — that is the point, and it is also the check that he has not locked himself
out before the archive is migrated.

## Rollback

```bash
# detach entirely
op run --env-file=.env.op -- terraform apply -var legacy_ghost_firewall_enabled=false
# or: keep the :22 fence, restore world HTTP if an unknown raw-IP consumer surfaces
op run --env-file=.env.op -- terraform apply \
  -var legacy_ghost_firewall_enabled=true -var legacy_ghost_http_public=true
```

Host-level rules on the box are untouched by any of this.

## While you are on the box

The exposure window here is ~20 months, far longer than GOL-2565's 13 days:

```bash
sudo grep -E 'Accepted (password|publickey)' /var/log/auth.log*
sudo last -F | head -40
```

This is an **exposure**, not evidence of compromise — nobody has looked for
compromise, and that cannot be done from the agent plane.

## Related

- **GOL-2565** — `grove-prod-odoo-fw` membership drift (`droplet_ids = []`). Different
  failure: that firewall *is* codified, so it converges on the next apply.
- **GOL-863** — Ghost snowflake retire. Premise corrected above; blocked on the
  content migration.
- **PR #745** — nightly firewall-membership watcher. Cannot cover this box: it
  asserts codified firewalls contain their codified droplets, and an un-codified
  droplet is invisible to it by construction. Once this firewall is codified and
  applied, the watcher does cover it.
