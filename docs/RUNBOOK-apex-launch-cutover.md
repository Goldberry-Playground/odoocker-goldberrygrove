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

**Recommended order:** flip the **hub (`gatheringatthegrove.com`) first** as the
canary (highest-traffic + fastest to eyeball), verify green, then the remaining
three. Each apex is independently reversible; do not batch the DNS edits so
faster.

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

### Step 3 — enable CF Redirect Rules apex → `blog.*` (Terra)
Bump the pre-launch apex redirect rules from **302 → 301** so the old apex blog
URLs permanently redirect to `blog.*`. (302 is the current pre-launch policy.)

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
- `infra/terraform/environments/production/blogs.tf` (`ghost_urls` canonical `blog.*`)
- `docs/RUNBOOK-blogs-reserved-ip-cutover.md` (Phase 2 = the Ghost-flip apply)
- `infra/terraform/environments/cloudflare-policy/` (Origin Rules / redirect style)
