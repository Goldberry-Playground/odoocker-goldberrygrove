# Runbook: Brand-apex launch cutover — 4 apexes → App Platform (GOL-287 / GOL-116c)

**Status:** READY, gated on CEO launch-go + a named change window. This is a
**ONE-WAY DOOR**, coordinated across **all four businesses**. It is not a silent
apply — the CEO schedules the window; **DevOps - Terra** executes the DNS/infra
legs; **Engineering - Ada** verifies each apex and owns rollback triggering.

Carved from the approved **GOL-116** plan (decision #2). Consolidates:
- the proven CF-proxied-apex Host-override recipe (**GOL-285 / GOL-116a**),
- the live App Platform ingress targets (**GOL-286 / GOL-824 / GOL-116b**),
- the Ghost canonical `blog.*` flip, which rides the **blogs-droplet Phase-2
  replace** (`docs/RUNBOOK-blogs-reserved-ip-cutover.md`).

---

## What this does

The four apexes today serve **Ghost** (blogs droplet, reserved IP, `A` records).
This flips each apex to its **App Platform** Next.js frontend and demotes Ghost
to `blog.*`:

| Apex | Business | App | App Platform ingress (Origin-Rule Host target) | App ID |
|------|----------|-----|------------------------------------------------|--------|
| `gatheringatthegrove.com` | Gathering at the Grove (hub) | `grove-hub-prod` | `grove-hub-prod-bpyrs.ondigitalocean.app` | `d5fa7795-da75-40e7-93fb-983e71558279` |
| `goldberrygrove.farm` | Goldberry Grove Farm | `grove-goldberry-prod` | `grove-goldberry-prod-efc9e.ondigitalocean.app` | `3da0b924-85f6-4531-859f-699e03c3cd74` |
| `woodworkingeorge.com` | Woodworking George / G3 | `grove-ggg-prod` | `grove-ggg-prod-r866z.ondigitalocean.app` | `30c2a739-97d2-43bf-a6f8-dfff4a318bd8` |
| `atthegrovenursery.com` | At The Grove Nursery | `grove-nursery-prod` | `grove-nursery-prod-peoxi.ondigitalocean.app` | `b9e0d2a6-6495-4dc7-a069-015b653c87e9` |

> ⚠️ **The ingress host contains a DO-assigned random suffix** (`-bpyrs`,
> `-efc9e`, …). It is not guessable and **changes if the app is destroyed/
> recreated.** Re-read each from `doctl apps get <app-id>` or the TF
> `*_default_ingress` output **at the top of the window** and confirm it matches
> the table before writing any Origin Rule. Values above verified 2026-07-26.

---

## GATES — all four required before touching a live apex

| Gate | Source | Status (2026-07-26) |
|------|--------|---------------------|
| CF-proxied-apex + Host-override recipe proven | GOL-285 | ✅ done — recipe below |
| App Platform apps live + ingress URLs captured | GOL-286 / GOL-824 | ✅ done — table above, all 4 served 200 + correct brand title |
| QA L3 soak sign-off green (ADR-007) | GOL-104 | ✅ **soak sign-off CLEAR 2026-07-23** (Terra corroborated w/ OpenObserve). NB: the GOL-104 *issue* may still read `blocked` for downstream rollout hygiene — the **soak gate itself is met** |
| **CEO launch-go + named change window** | — | ⛔ **PENDING — the only open gate.** Escalate/confirm immediately before execution |

---

## Recipe — the Host-override mechanic (proven, GOL-285)

Under **Full (strict)**, Cloudflare connects to the origin with **SNI = the CNAME
target** (`*.ondigitalocean.app`) and forwards an HTTP `Host` header that
**defaults to the visitor hostname (the brand apex)** unless an **Origin Rule**
rewrites it. DO's App Platform edge routes purely by recognized `Host` and
**fails closed with `403` (`server: cloudflare`, no `x-do-app-origin`)** if the
`Host` is not a host it recognizes. Therefore the Origin Rule is **mandatory,
not optional**. TLS "just works": DO presents a publicly-valid Google Trust
Services cert with SAN `*.ondigitalocean.app` matching the SNI CF sends — no
Origin CA cert, no SNI override needed.

Per apex, in the **apex's own Cloudflare zone**:

1. **DNS** — proxied (orange-cloud) `CNAME <apex> → <app>.ondigitalocean.app`
   (CF CNAME-flattening handles the root apex). Ship **no `domain{}`** block in
   the App Platform spec (external-CDN pattern; app keeps only default ingress).
2. **SSL/TLS** — zone mode **Full (strict)**.
3. **Origin Rule** (Rules → Origin Rules → Create, one per apex):
   - **When**: `Hostname` `equals` `<apex>` (add `or www.<apex>` if that host is
     also proxied to the same app).
   - **Then → Set the Host Header → Rewrite to**: `<app>.ondigitalocean.app`.

Terraform form (matches `environments/cloudflare-policy` style — Origin Rules
run in the `http_request_origin` phase):

```hcl
resource "cloudflare_ruleset" "app_host_override" {
  for_each = var.apex_ingress   # { "gatheringatthegrove.com" = "grove-hub-prod-bpyrs.ondigitalocean.app", ... }
  zone_id  = data.cloudflare_zone.zones[each.key].id
  name     = "App Platform host override"
  kind     = "zone"
  phase    = "http_request_origin"

  rules {
    action = "route"
    action_parameters { host_header = each.value }   # *.ondigitalocean.app default ingress for this apex's app
    expression  = "(http.host eq \"${each.key}\")"    # add: or www
    description = "Rewrite Host to App Platform ingress so DO edge routes (else 403). GOL-116/GOL-285/GOL-287."
    enabled     = true
  }
}
```

---

## Execution — one change window, four apexes

**Recommended order (revised, GOL-1282):** canary with a **currently-522 apex —
`atthegrovenursery.com` (nursery)** — **first**, not the flagship hub.

Rationale: ggg + nursery apexes today return **`522`** (no working origin), so
flipping one to a working App Platform storefront is **zero-regret** — a failed
attempt changes nothing a user sees, while a green result exercises the full,
novel **DNS-repoint + Host-override Origin Rule** mechanic (the `403`-fail-closed
risk) live before we touch a flagship. Sequence:

1. **nursery** (522 today) — canary the Origin-Rule mechanic. No redirect rule to
   narrow (Step 3 N/A for nursery).
2. **hub** (`gatheringatthegrove.com`) — flagship, highest-traffic; de-risked by
   the canary. Has the redirect rule (Step 3 applies).
3. **goldberry** (`goldberrygrove.farm`) — has the redirect rule (Step 3 applies).
4. **ggg** (`woodworkingeorge.com`, 522 today).

Each apex is independently reversible; do not batch the DNS edits to go faster.
> Note: Step 3's `redirects.tf` flag covers hub **and** goldberry in one state —
> enable it only once **both** their DNS repoints (steps 2 + 3 above) are green.

### Step 0 — pre-flight (Ada + Terra, top of window)
- [ ] Re-read all four `*_default_ingress` hosts; confirm they match the table.
- [ ] Confirm all four ingresses serve **200 + correct brand `<title>`** directly
      on `*.ondigitalocean.app` (baseline before any DNS move).
- [ ] Record the current apex `A` records (rollback target) — see Rollback.
- [ ] Blogs Phase-2 pre-flight (see Step 2): snapshot + backup restore-check.

### Step 1 — repoint apex DNS (Terra, per apex)
1. In the apex zone, replace the apex `A → blogs-reserved-IP` with a **proxied
   `CNAME <apex> → <app>.ondigitalocean.app`** (table above).
2. Confirm zone SSL/TLS = **Full (strict)**.
3. Create/enable the **Origin Rule** Host override for this apex (recipe above).
4. **Verify before moving to the next apex** (see Verify).

### Step 2 — flip Ghost canonical to `blog.*` (Terra, once for all four)
The canonical `url` for all four Ghost instances is already set to `blog.*` in
`infra/terraform/environments/production/blogs.tf` (`ghost_urls`, EOM-July
policy). **Applying it edits `user_data` ⇒ `digitalocean_droplet.blogs` is
REPLACED (ForceNew)** — this IS **Phase 2** of
`docs/RUNBOOK-blogs-reserved-ip-cutover.md`: ~10–20 min blogs outage, volume +
reserved IP survive. **Do the snapshot + backup restore-check first.** ggg +
nursery are already headless; hub + goldberry are the ones actually flipping off
their apex.

### Step 2b — flip the blog.* Caddy headless-demote flag (Terra, SAME replace as Step 2)

The blog.* Caddy vhosts (blogs droplet, `compose/Caddyfile-blogs.tpl`) proxy the
reader-facing Ghost site today. Post-cutover they must be **headless**: pass
through only `/ghost` + `/ghost/*` (admin + Admin/Content API), `/content/*`
(images), `/members/*`, and **301** every other path to the brand's React blog
route on the apex. This is gated on **`var.blog_headless_demote_enabled`**
(default `false`, blogs.tf → GOL-1530).

> ⚠️ **This flag feeds `user_data`, so it activates ONLY on a droplet REPLACE — the
> SAME replace Step 2 already schedules.** Do **not** spend a standalone replace on
> it. Set it in the same apply as the Step-2 Ghost url-flip:
>
> ```bash
> op run --env-file=.env.op -- terraform apply \
>   -replace=digitalocean_droplet.blogs \
>   -var=blog_headless_demote_enabled=true
> ```
>
> (Or commit the default flip to `true` in the cutover-window PR — GOL-1471-style —
> and run the plain `-replace` apply. Either way it rides Step 2's single replace.)

> ⚠️ **Ordering — do NOT flip before the apex DNS repoint (Step 1) is green for the
> flagged brand.** The apex side (redirects.tf `/content/*` 301, Step 3) plus this
> blog 301 must not both be live while the apex still resolves to Ghost, or readers
> loop: apex 302 → blog 301 → apex. Safe order per brand: Step 1 (apex → App
> Platform) → Step 2 + 2b (blogs replace, canonical `blog.*` + headless) → Step 3
> (narrow apex `/content/*` 301). hub → `/journal/{slug}`; goldberry →
> `/blog/{slug}`; ggg + nursery → apex root (no per-slug map — GOL-1113/GOL-1284).

Post-activation verify (in the window, per brand):
```bash
# Reader path 301s to the matching React route (NOT Ghost HTML)
curl -sSI https://blog.goldberrygrove.farm/some-post | grep -iE 'HTTP/|location'
#   want: 301 + location: https://goldberrygrove.farm/blog/some-post
curl -sSI https://blog.gatheringatthegrove.com/some-post | grep -iE 'HTTP/|location'
#   want: 301 + location: https://gatheringatthegrove.com/journal/some-post
# Ghost admin still reachable (passthrough)
curl -sSI https://blog.goldberrygrove.farm/ghost/ | grep -i 'HTTP/'   # 200/302 from Ghost, NOT 301-to-apex
# Embedded images still serve (Content API surface unaffected)
curl -sSI https://blog.goldberrygrove.farm/content/images/ | grep -i 'HTTP/'
```
Also confirm the storefront `/blog` (and hub `/journal`) pages still render — they
read Ghost via the **Content API** (`/ghost/api/content/*`, in the passthrough
allowlist), so the demote does not touch them.

**Rollback:** re-apply with `-var=blog_headless_demote_enabled=false` (default) on
the next replace, OR revert the committed flip — reverses to full passthrough.

### Step 3 — narrow the apex redirect to `/content/*` only, then enable (Terra)

> ⚠️ **DO NOT "bump 302 → 301" on the blanket rule.** The pre-launch rule matches
> `(http.host eq <apex>)` — i.e. **every** path, including `/`. It fires in the
> `http_request_dynamic_redirect` phase, which runs **before** origin selection,
> so while it is enabled the apex DNS repoint (Step 1) does **nothing** — the app
> is never reached. Bumping it to 301 would make that shadowing **permanent and
> browser-cached**, poisoning the flagship apex. Fixed in **GOL-1282**.

The blanket rule is already **replaced** in `redirects.tf` by a narrow, permanent
`apex/content/* → blog.<apex>/content/*` **301** (embedded Ghost assets only;
`/content/` is never a storefront route). Everything else — the apex root and all
storefront routes — falls through to origin selection and reaches the App
Platform app. Applies to **hub + goldberry** only (ggg + nursery never had a rule).

Execute, per apex, **after** Step 1 (DNS repoint) is verified for that apex and
Step 2 (blog.* serving Ghost) is done:

```bash
op run --env-file=.env.op -- terraform apply -var=blog_apex_redirects_enabled=true
```

- `redirects.tf`'s `cloudflare_ruleset.blog_apex_redirect` covers both hub and
  goldberry in one state; the flag flips both. Enable it only once **both** their
  apex DNS repoints (Step 1) are green, so neither apex root is shadowed by a
  stale-DNS + enabled-rule overlap.
- **Legacy blog post-paths** (`apex/<slug>` → `blog.<apex>/<slug>`) are **not**
  redirected: post slugs are indistinguishable from storefront routes at the CF
  edge, so they need an explicit slug allow-list. Deferred to **GOL-1284**; old
  post links **404 on the apex** until it ships (accepted at launch — the blog
  lived at the apex for only the ~1-month EOM-July QA window).

### Step 4 — purge Cloudflare cache (Terra, per zone)
Purge Everything for each of the four zones after its apex is cut over and
verified, so no stale Ghost HTML is served from edge cache.

---

## Verify (Ada, per apex — do NOT proceed to the next apex until green)

```bash
# 1. Apex serves the App Platform frontend (200, via CF -> Origin-Rule -> app)
curl -sSI https://<apex>/            # expect: HTTP/2 200
curl -s  https://<apex>/ | grep -i '<title>'   # expect the App Platform brand title, NOT Ghost

# 2. Confirm the request actually reached the DO app (not edge-cached Ghost)
curl -sSI https://<apex>/ | grep -iE 'cf-ray|x-do-orig-status|server'
#   want: cf-ray present AND x-do-orig-status: 200
```

**Failure signature → Origin Rule missing/mis-pointed:** `403` with
`server: cloudflare` and **no `x-do-app-origin`** header → the Host was not
rewritten to the ingress. Fix the Origin Rule (or roll back this apex) before
continuing.

Expected brand titles (App Platform frontends, not DO placeholders):
- hub → "Gather at the Grove — …"; goldberry → Goldberry Grove Farm; ggg →
  Woodworking George; nursery → At The Grove Nursery.

Also confirm `https://blog.<apex>/` still serves Ghost 200 after Step 2.

---

## Rollback (per apex — CF-proxied, ~instant)

The DNS leg is the one-way door's reversible half: **revert the single apex
record** `CNAME → <app>.ondigitalocean.app` back to the recorded
`A → <blogs-reserved-IP>`. CF-proxied TTL is effectively instant. Optionally
disable that apex's Origin Rule. Purge cache. Each apex rolls back
independently — a failed hub flip does not block or require touching the other
three.

- **Ghost canonical (Step 2) rollback** is heavier (another blogs-droplet
  replace) — prefer **not** to run Step 2 until at least the hub apex flip is
  verified green, so a DNS-only rollback is sufficient for the common failure.
- Record the four current apex `A` targets in Step 0 so the rollback value is
  captured, not reconstructed under pressure.

---

## Ownership

| Leg | Owner |
|-----|-------|
| Launch-go decision + change-window scheduling | **CEO - Rick** |
| DNS records, Origin Rules, SSL mode, `blogs.tf` Phase-2 apply, Redirect Rules, cache purge | **DevOps - Terra** (infra seat) |
| Ingress-target correctness, per-apex verification, rollback trigger, this runbook | **Engineering - Ada** |

## References
- GOL-287 (this cutover) · GOL-116 (approved plan) · GOL-285 (recipe) ·
  GOL-286 / GOL-824 (ingress URLs) · GOL-104 (soak) · GOL-817 (rollout umbrella)
- GOL-1528 / GOL-1530 (blog.* Caddy headless-demote flag — Step 2b) ·
  GOL-1113 / GOL-1284 (deferred per-slug maps for nursery / apex post-paths)
- `infra/terraform/environments/production/blogs.tf` (`ghost_urls` canonical `blog.*`)
- `infra/terraform/environments/production/compose/Caddyfile-blogs.tpl` (blog.* vhosts; `var.blog_headless_demote_enabled`)
- `docs/RUNBOOK-blogs-reserved-ip-cutover.md` (Phase 2 = the Ghost-flip apply)
- `infra/terraform/environments/cloudflare-policy/` (Origin Rules / redirect style)
