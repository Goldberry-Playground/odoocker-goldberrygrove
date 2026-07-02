# Ghost CMS in QA

QA runs one Ghost instance per tenant (goldberry / ggg / nursery). Each has
its own SQLite database inside its container's volume and is reachable only
from inside the Docker network — storefronts fetch posts server-side via the
Content API, so there's no public HTTPS endpoint for Ghost, no Caddy vhost,
and no cert to worry about.

Landed in Phase C on 2026-07-01 (Asana #97). Before that, QA had zero Ghost
containers and storefronts either pulled from prod `goldberrygrove.farm`
(coupling QA to prod) or fetched from a stub URL that always failed.

## What's in the compose

`infra/terraform/environments/qa/compose/docker-compose.qa.yml` defines:

| Service | Internal URL | Volume |
|---|---|---|
| `ghost-goldberry` | `http://ghost-goldberry:2368` | `ghost-goldberry-data` |
| `ghost-ggg` | `http://ghost-ggg:2369` | `ghost-ggg-data` |
| `ghost-nursery` | `http://ghost-nursery:2370` | `ghost-nursery-data` |

Volumes are **ephemeral** — content resets on droplet recreate. That's
intentional for QA; when we need durability, follow the pattern in
[`docs/ADR/005-qa-cert-resilience-stack.md`](./ADR/005-qa-cert-resilience-stack.md)
and mount them onto the persistent block volume that already hosts
`caddy-data`.

## Bootstrap (one-time, per fresh QA droplet)

Fresh Ghost containers boot with no admin user and no API keys. Storefronts
need Content API keys to fetch posts, so after a droplet recreate you'll see
`GHOST_CONTENT_KEY=qa-stub-no-ghost-key-yet` — that's the stub sentinel.
Seed real keys with:

```bash
# 1. SSH to the QA droplet (see qa-deploy Discord post for the IP).
ssh -i ~/.ssh/grove-qa-admin root@<droplet_ip>

# 2. Run the seed wrapper. Any password works -- QA is ephemeral.
GHOST_ADMIN_PASSWORD='QaEphemeralPw!2026' \
  bash /workspace/current/setup-all-ghosts.sh
```

The wrapper:

1. Waits up to 90s for each Ghost container to respond
2. Calls `setup_ghost_integration.py` (from the grove-odoo-modules git-sync
   mount) once per tenant to create the admin user + a Content Integration
3. Prints an `infisical secrets set ...` block for you to paste on your
   operator laptop (where `op` auth is alive)

## Push the keys to Infisical + reload storefronts

Copy the `infisical secrets set` block from the wrapper's output and run it
on your laptop. Then either:

**Fast path** (no rebuild, ~10 sec):
```bash
ssh -i ~/.ssh/grove-qa-admin root@<droplet_ip> \
  'docker restart hub goldberry ggg nursery'
```
Storefronts re-read the sentinel values on restart. Wait ~30s for Next.js
warmup.

**Full path** (rebuilds droplet, ~15 min): `gh workflow run "QA Deploy"`.

## Verify

```bash
# Storefronts should now render real /blog pages
curl -sI https://goldberry.qa.gatheringatthegrove.com/blog | head -3
curl -sI https://ggg.qa.gatheringatthegrove.com/blog | head -3
curl -sI https://nursery.qa.gatheringatthegrove.com/blog | head -3
```

Expect HTTP 200. If a `/blog` page renders "no posts," the key wiring is
fine but the Ghost has no published posts yet — log into
`http://ghost-goldberry:2368/ghost/` via an SSH tunnel and publish a test
post:

```bash
ssh -i ~/.ssh/grove-qa-admin -L 2368:ghost-goldberry:2368 root@<droplet_ip>
# Then browse http://localhost:2368/ghost/ on your laptop.
```

## Idempotency

- The seed wrapper is safe to re-run. `setup_ghost_integration.py` detects
  a completed setup and skips it; the integration lookup is name-based, so
  the same wrapper on a re-run reuses the existing integration and prints
  the SAME keys — no drift.
- `infisical secrets set` upserts by name, so the seed block is safe to
  re-run too.

## Why manual (for now)

Automating the seed inside cloud-init is tracked as [Asana
#117](https://app.asana.com/) — it needs to (a) wait for Ghost health, (b)
call setup_ghost_integration.py, (c) push keys to Infisical via CLI from
the droplet, (d) restart storefronts. All doable, but adds ~150 lines of
cloud-init and 3-4 new failure modes. For QA — where a fresh seed is a
5-min operator step and droplet recreates aren't hourly — the manual flow
is honest.

When we cutover to Level 3 (App Platform + Managed PG per ADR-007), Ghost
will move to a managed service anyway. #117 stays deferred until then
unless QA recreates get frequent.

## Hub's editorial feed

The hub's journal pages (`apps/hub/app/journal/*.tsx`) pull from
`ghost-goldberry` in QA too, so hub's editorial content is testable without
touching prod. In prod (post-Level-3), hub will point at a dedicated
editorial Ghost — see ADR-007 Phase 6.
