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

The Phase-4 CMO pipeline re-hosts operator-supplied social media (Discord/Drive drops, Canva exports) into a durable public URL that Buffer fetches at publish time (the `AssetStore` seam in `grove-sites` `apps/discord-bridge/lib/ingest.ts`).

**Decision: these assets land in `grove-assets` under the `social/` prefix** — they are marketing imagery not tied to a product or a blog post, i.e. exactly this bucket's charter. Considered and rejected:

- **Extend the Odoo filestore (spike option A).** Would reuse the EXIF-strip recipe, but requires `grove_headless` to mint a durable, unauthenticated public URL for arbitrary (non-product) attachments. Unnecessary: `grove-assets` already mints exactly that URL. Ties transient marketing media to the product system of record for no gain.
- **A brand-new dedicated Spaces bucket (spike option B, literal reading).** Would create a second marketing-asset source of truth and new bucket/CDN/DNS to provision and monitor. `grove-assets` already exists for this content class.

Chosen path is spike option B's *storage model* (S3, trivially public-read, CDN-fronted) with **zero new bucket/CDN/DNS** — reuse the existing one under a new prefix.

Infra provisioned for it (`infra/terraform/environments/assets`):

- `digitalocean_spaces_key.assets_social_rw` — a **separate, bucket-scoped RW key** for the discord-bridge, distinct from the operator upload key (`assets_rw`). DO Spaces keys are bucket-scoped, not prefix-scoped, so the grant is bucket-wide readwrite; the isolation is on the **identity** axis — the automated service and the human operator rotate/revoke independently, and a leaked bridge key never touches the operator flow.
- Its access/secret land in 1Password `Grove Infra` as `grove_asset_store_key` / `grove_asset_store_secret`, injected into the bridge as `GROVE_ASSET_STORE_KEY` / `GROVE_ASSET_STORE_SECRET`.

The bridge's `SpacesAssetStore` writes objects with `x-amz-acl: public-read` and returns `https://assets.gatheringatthegrove.com/social/<sha256>.<ext>` — a clean https URL with no query string, so the media contract's short-lived-signed-URL guard passes by construction.

## Consequences

- **Positive:** one asset store for all marketing media; no new infra to provision, monitor, or bill; the re-host seam satisfies its durable-public-URL requirement trivially; EXIF/GPS strip is enforced upstream by the ingest seam before any byte reaches the bucket.
- **Trade-off:** the bridge key carries bucket-wide RW (DO's granularity limit), mitigated by identity separation + content-hash keys (idempotent, no path traversal). Social objects share the bucket with brand imagery, namespaced by the `social/` prefix.
- **Follow-up:** lifecycle/retention on the `social/` prefix (do re-hosted posts expire?) is deferred — assets are small and public; revisit if bucket size warrants.
