# ADR-009: Grove Asset Storage — Tiered Media Strategy

**Status:** Proposed
**Date:** 2026-06-26

## Context

Binary media (photography, logos, video, design-system sample imagery) is stored across four+ uncoordinated places with no single source of truth per asset class:

- **git `apps/*/public/`** — Goldberry farm/founders/falls photos, nursery heroes + products, a drone poster. Bloats the repo (a 564 KB webp was committed), has no optimization pipeline, isn't shared across apps, and lives in history forever.
- **Unsplash hotlinks** — GGG's entire hero and the original design-system previews. Not owned, can change or disappear, rate-limited, and **broke the design-system audit**: a remote URL failed inside CSS `url()` because a non-base64 encoding left a stray quote that terminated the `background-image` string (blank hero).
- **Odoo** — intended home for product/catalog images (served via the BFF, ADR-002), but some product images also sit in git `public/products/` → drift.
- **Ghost** — intended home for blog/journal imagery, not consistently used.
- The actual drone **video** is unaccounted for (only the poster is in git).

The root cause is that asset **classes** aren't routed to the system built for them, and a third-party hotlink stands in for owned photography.

## Decision

Adopt a **tiered model** that routes each asset class to infrastructure already in the stack (no new vendor), with **env-configured base URLs** so prod / QA / preview resolve correctly.

1. **Editorial / blog images → Ghost.** The CMS handles uploads, responsive sizes, and serving. Editorial imagery never enters git.
2. **Product / catalog images → Odoo.** The product owns its media; the BFF (ADR-002) serves it. Migrate stray `public/products/*` into Odoo.
3. **Brand / marketing static (heroes, about, founders) + all video → DigitalOcean Spaces + CDN.** Upload via an optimize-on-ingest script; reference via `NEXT_PUBLIC_ASSET_BASE`. The drone video lives here.
4. **Logos + a canonical brand-asset manifest → a `@grove/brand` package.** Small, **typed references** to the CDN (not the binaries). Apps and the design system both import it; a missing/renamed asset becomes a type error, not a 404.
5. **Design-system sample imagery → embedded** (small, owned, optimized, base64). No remote dependency in the render sandbox — already implemented (`_sample-images.ts`, 2026-06-26).

**`NEXT_PUBLIC_ASSET_BASE` contract:** each app reads the base from the secrets pipeline (Infisical, ADR-003); asset refs are `${base}/<path>`, never hardcoded hosts, varying per env like sibling-site URLs already do.

**Ingest pipeline:** a `scripts/upload-asset.ts` (Sharp): resize → webp/avif → responsive widths → upload to Spaces (public-read, long cache headers, content-hashed names). One command per asset; output is the CDN path.

## Consequences

**Benefits:**
- No third-party hotlinks anywhere — owned assets only, in prod and design (kills the audit-breaking failure mode).
- One source of truth per class; no git/Odoo drift.
- Repo stays lean — git holds only small, stable, structural assets (logos, DS samples); never photo libraries or video.
- CDN-fast delivery; modern formats + responsive sizes guaranteed at ingest.
- `@grove/brand` makes brand assets a typed, shared, versioned surface for apps and the design system.

**Trade-offs:**
- New moving parts: a Spaces bucket + CDN, an ingest script, and a `@grove/brand` package to build and maintain.
- A migration is required (move `public/` photos out, products into Odoo, blog images into Ghost, video into Spaces).
- Asset refs gain an env-var dependency (`NEXT_PUBLIC_ASSET_BASE`) that must be wired per env.
- **Surfaces a content gap:** GGG has no owned photography (Unsplash stock) — a real shoot is required before its assets can flow through Tier 3. This is a content/brand task, not infrastructure.

**Migration (phased):** (1) Spaces + CDN + `upload-asset.ts`, prove on goldberry. (2) `@grove/brand`. (3) blog → Ghost, products → Odoo. (4) drone video → Spaces. (5) backfill GGG photography.

**References:** ADR-002 (BFF), ADR-003 (Infisical secrets), ADR-007 (App Platform). Full spec + open questions in the AgenticOS vault: *Grove Asset Storage*.
