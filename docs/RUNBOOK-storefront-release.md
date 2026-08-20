# Runbook — Production Storefront Release (App Platform) — Option A appendix

**Owner:** DevOps (Terra) · **Related:** GOL-1325 (release-flow exercise), GOL-1607
(stale-storefront incident), GOL-1304 (pin-SHA deploy policy), GOL-1600 (codify).

> **Consolidated 2026-08-20.** This file and grove-sites'
> [`docs/runbooks/prod-frontend-deploy.md`](https://github.com/Goldberry-Playground/grove-sites/blob/main/docs/runbooks/prod-frontend-deploy.md)
> were written independently to solve the same problem. **grove-sites' doc is now
> canonical** — it has the actual reusable script
> (`scripts/lib/do-app-redeploy.sh`), the automated drift alarm, and this file's
> fingerprint-verification method and 2026-08-18 incident record, ported over.
>
> This file is now a **short appendix**: it exists only for the Option-A-specific
> pin-bump step, which lives in *this* repo's Terraform, not grove-sites'. For the
> actual deploy mechanism, the two hard lessons, and the verification method, go
> to the canonical doc above (or its automated form:
> `.github/workflows/promote-storefronts.yml`, this repo).

## Why this appendix exists

`docs/RELEASE.md` + `.github/workflows/release.yml` deploy **only the Odoo
compose stack** (SSH → `git checkout <tag>` → `docker compose pull/up`). They do
**not** roll the four revenue storefronts, which run as DigitalOcean App
Platform apps pulling GHCR images. Cutting a `vX.Y.Z` tag updates Odoo and
leaves the storefronts untouched — that's the gap the canonical doc's
"Promoting a new build" section and `promote-storefronts.yml` close.

## The one step that's genuinely odoocker-specific: bumping the pin

Under Option A (GOL-1304), prod pins `hub_image_tag` / `tenant_image_tag` in
`infra/terraform/environments/production/variables.tf` to immutable 40-char
commit SHAs — validated by regex, never a moving tag. Bumping the pin is a
reviewed PR in *this* repo:

1. Update the var default(s) to the new SHA (the build published by grove-sites
   CI).
2. `terraform plan` (targeted to the four `digitalocean_app` resources) →
   review → `terraform apply`.
3. Go to grove-sites' canonical runbook (or run `promote-storefronts.yml`) for
   step 3 onward — the actual `create-deployment` roll, verification, and
   notify.

**A green `terraform apply` here is not evidence of a deploy** (Lesson 1 in the
canonical doc) — step 3 is not optional.

## Production app IDs

| App | App Platform ID | Public URL |
|---|---|---|
| grove-hub-prod | `d5fa7795-da75-40e7-93fb-983e71558279` | https://gatheringatthegrove.com |
| grove-nursery-prod | `b9e0d2a6-6495-4dc7-a069-015b653c87e9` | https://atthegrovenursery.com |
| grove-goldberry-prod | `3da0b924-85f6-4531-859f-699e03c3cd74` | https://goldberrygrove.farm |
| grove-ggg-prod | `30c2a739-97d2-43bf-a6f8-dfff4a318bd8` | https://woodworkingeorge.com |

## Validation record

- **2026-08-18 — QA rehearsal (GOL-1325):** exercised the mechanism against
  `grove-ggg-qa`. Reproduced the double-deploy trap for real (now Lesson 2 in
  the canonical doc, with the exact failure mode recorded there).
- **2026-08-20 — first real prod promotion under Option A:** the pin PR (#536)
  merged 2026-08-18 but sat un-applied against prod for two days, caught by the
  drift alarm. Applied + redeployed all four apps 2026-08-20 — see the
  canonical doc's Validation record for the full account. That gap is the
  direct motivation for `promote-storefronts.yml`.
