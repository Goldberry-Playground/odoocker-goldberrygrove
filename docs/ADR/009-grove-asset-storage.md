# ADR 009: Grove asset storage — one shared Spaces bucket for non-product, non-editorial media

**Status:** Accepted
**Date:** 2026-08-02
**Deciders:** DevOps-Terra (pending a one-line CEO nod on the social-ingest amendment)
**Reference:** [`docs/ASSETS.md`](../ASSETS.md), `infra/terraform/environments/assets/`

## Context

Grove media lives in three homes, by kind:

| Content kind | Home | URL pattern |
|---|---|---|
| **Product photos** | Odoo filestore, served by `grove_headless` | `${ODOO_URL}${imageUrl}` |
| **Blog post images** | Ghost | Content API returns the URL |
| **Everything else** (hero/brand imagery, illustrations, logos, marketing) | **`grove-assets` DO Spaces bucket** | `https://assets.gatheringatthegrove.com/<tenant>/<path>` |

The `grove-assets` bucket is `public-read`, Cloudflare-proxied + DO-CDN-fronted, and Terraform-managed (`infra/terraform/environments/assets`). This was already the shipped state (`docs/ASSETS.md`); ADR-009 formalizes it as the recorded decision and hosts amendments as new asset producers appear.

## Decision

**The `grove-assets` Spaces bucket is the canonical store for any Grove media that is not a product photo (Odoo) or a blog image (Ghost).** It is public-read by design — nothing secret is ever written to it — and served through the Cloudflare Worker vanity host `assets.gatheringatthegrove.com` (durable, unauthenticated, no signed query params).

## Amendment 2026-08-02 — social-media ingest re-host (GOL-1120 / GOL-1119)

The Phase-4 CMO pipeline re-hosts operator-supplied social media (Discord/Drive drops, Canva exports) into a durable public URL that Buffer fetches at publish time (the `rehostToMediaAsset` seam in `grove-sites`).

**Storage decision: these assets land in `grove-assets` under the `social/` prefix** — they are marketing imagery not tied to a product or a blog post, i.e. exactly this bucket's charter. Considered and rejected:

- **Extend the Odoo filestore (spike option A).** Would reuse the EXIF-strip recipe, but requires `grove_headless` to mint a durable, unauthenticated public URL for arbitrary (non-product) attachments. Unnecessary: `grove-assets` already mints exactly that URL. Ties transient marketing media to the product system of record for no gain.
- **A brand-new dedicated Spaces bucket (spike option B, literal reading).** Would create a second marketing-asset source of truth and new bucket/CDN/DNS to provision and monitor. `grove-assets` already exists for this content class.

Chosen path is spike option B's *storage model* (S3, trivially public-read, CDN-fronted) with **zero new bucket/CDN/DNS** — reuse the existing one under a new prefix.

### Execution model — reuse `@grove/assets` in apps/hub, no new key (revised GOL-1122 / GOL-1123)

The original amendment scoped a hand-rolled `SpacesAssetStore` (SigV4 PutObject) in the discord-bridge, with its own `assets_social_rw` Spaces key. **That is superseded.** Two facts forced the revision (Ada's Lead-Eng call on GOL-1122, reconciled by DevOps on GOL-1123):

1. **The bridge can't run the privacy step.** `apps/discord-bridge` is zero-runtime-dependency by design (its Dockerfile copies only the app dir, runs no install, `node server.ts` strips TS natively). The EXIF/GPS strip — a stop-ship privacy requirement — needs native `sharp`, which cannot run in that image.
2. **The whole pipeline already ships in `@grove/assets`.** `optimizeToVariants` (sharp; EXIF dropped on re-encode) + `uploadAsset` / `createSpacesAssetPipeline` write public-read to `grove-assets` (`https://assets.gatheringatthegrove.com/...`) via `spacesConfigFromEnv`, and are already surfaced through `apps/hub/app/api/assets/optimize/route.ts` (`runtime = "nodejs"` because sharp is native).

**Execution decision:** run the `rehostToMediaAsset` seam (normalize + store) in a small Next Node route in **apps/hub** (sibling of `/api/assets/optimize`, `social/` prefix), reusing `@grove/assets` for both the sharp normalize and the Spaces `PutObject`. The discord-bridge stays zero-dep and just forwards the raw drop to that route over HTTP. Consequences for infra:

- **No `SpacesAssetStore`, no hand-rolled SigV4** — `uploadAsset` already does the public-read `PutObject` with `ACL: public-read`, returning `https://assets.gatheringatthegrove.com/social/<content-hash>...` (clean https, no query string, so the media contract's short-lived-signed-URL guard passes by construction).
- **No new Spaces key.** The re-host runs in the same hub process that already holds the operator credential `GROVE_ASSETS_KEY` / `GROVE_ASSETS_SECRET` (in hub's deploy env for the live optimize route). A second key consumed by the same process buys no real isolation — same env, same blast radius — while adding a terraform apply, a 1Password secret, and a CEO gate. So `assets_social_rw` is **not** provisioned. (If the re-host is ever split into its own service, revisit identity separation then.)
- **Shared auth:** the bridge → hub call reuses the existing bearer `GROVE_ASSETS_OPTIMIZE_TOKEN` that the optimize route already validates (`checkAuth`); the token value is copied into the bridge's deploy env — a shared-secret copy, not a new credential to mint.

## Consequences

- **Positive:** one asset store for all marketing media; **zero new infra** — no new bucket/CDN/DNS, no new Spaces key, no new 1Password secret, no CEO money/secret gate; the re-host seam satisfies its durable-public-URL requirement trivially; EXIF/GPS strip is enforced by `@grove/assets`' sharp re-encode before any byte reaches the bucket.
- **Trade-off:** the social re-host shares the hub operator key's bucket-wide RW rather than a distinct identity. Acceptable because both callers live in the same hub process; identity separation would be meaningful only if the re-host became a standalone service. Social objects share the bucket with brand imagery, namespaced by the `social/` prefix.
- **Follow-up:** lifecycle/retention on the `social/` prefix (do re-hosted posts expire?) is deferred — assets are small and public; revisit if bucket size warrants.
