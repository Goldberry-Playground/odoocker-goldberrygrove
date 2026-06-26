# Grove Production — Terraform env

## ⚠️ DO NOT DEPLOY YET

Production deployment is **deferred** pending the Level 3 architectural rethink in [`docs/ADR/007-level-3-app-platform-migration.md`](../../../../docs/ADR/007-level-3-app-platform-migration.md).

The TF resources in this directory (`module "app"`, `module "monitoring"`, the DNS records map in `main.tf`) reflect the **pre-Level-3 design** — a monolithic droplet pattern that the architectural review on 2026-06-26 concluded should NOT be the prod target. The right shape for production is:

- DO App Platform for frontends (hub + 3 tenants)
- DO Managed Postgres for the database
- A tiny droplet for Odoo only (single-host Caddy in front, single LE cert)
- Same shape as QA after its Level 3 migration validates

Running `terraform apply` against this env now would half-provision the OLD architecture and create technical debt that Phase 6 of ADR-007 will throw away.

## When can this be deployed?

When **all three** are true:

1. Tonight's QA cert resilience PRs (#95-#103) have validated end-to-end on a real droplet (gates on LE rate limit clearing ~21:30 UTC 2026-06-27)
2. Level 3 Phase 1-5 have completed: a new `qa-app-platform/` env exists, has run for ~2 weeks without major issues, and the DNS cutover has succeeded
3. Phase 6 of ADR-007 has rewritten this env's `main.tf` to use App Platform + Managed PG (not the current `modules/droplet` calls)

When that PR lands, the banner at the top of `main.tf` and this README's "DO NOT DEPLOY YET" section should be removed in the same diff.

## Why the QA work doesn't apply here directly

Tonight (2026-06-26) the QA env shipped a 4-PR cert resilience stack (`docs/ADR/005-qa-cert-resilience-stack.md`):

- **PR #95** — persistent Caddy `/data` volume (avoids LE rate limits on droplet recreates)
- **PR #96** — branch-aware ACME endpoint (operator can flip to LE staging)
- **PR #97** — orphan `_acme-challenge` TXT cleanup preflight (workaround for caddy-dns/digitalocean delete bug)
- **PR #98** — Caddy multi-issuer fallback (auto-fallback to staging on prod 429)

These are scoped to QA's TLS stack: **Caddy + DNS-01 wildcard + DO DNS plugin**. Production-as-currently-designed in `main.tf` (and per `docs/DEPLOY.md`) uses **nginx + manual ACME via Cloudflare DNS** — a completely different stack. The QA resilience layers do not transfer; they'd need to be re-implemented for nginx, which is not worth doing because:

1. Level 3 makes most of them inert anyway (App Platform handles TLS for frontends)
2. The remaining cert dependency in Level 3 (one cert for the Odoo host) has trivial rate-limit exposure (1 cert / ~80 days)

So instead of backporting tonight's PRs to prod-as-currently-designed, the structural answer is: replace prod's design before deploying.

## What this directory currently scaffolds (for historical reference)

| Resource | Notes |
|---|---|
| `module "app"` | App droplet (`s-4vcpu-8gb` + 100GB volume) via `modules/droplet` |
| `module "monitoring"` | Monitoring droplet (`s-2vcpu-4gb` + 20GB volume) via `modules/droplet` |
| `digitalocean_firewall.app` | 80/443 public + SSH restricted |
| `digitalocean_firewall.monitoring` | SSH + 3000 (Grafana) restricted |
| DNS records | Defined in `locals.dns_records` but actual records are out-of-band in Cloudflare |
| Cloud-init | NONE — bootstrap is manual per `docs/DEPLOY.md` |
| Caddy / Caddyfile | NONE — production uses nginx (in compose) for TLS termination |

This scaffolding is correct for the pre-Level-3 design. Don't delete it yet (might inform Phase 6's rewrite of which envs need which sizes), but don't rely on it being the final shape.

## Cross-references

- [`docs/ADR/005`](../../../../docs/ADR/005-qa-cert-resilience-stack.md) — the QA work that exposed the gap
- [`docs/ADR/006`](../../../../docs/ADR/006-hub-qa-subdomain-not-apex.md) — URL convention divergence (QA hub on subdomain, prod hub on apex)
- [`docs/ADR/007`](../../../../docs/ADR/007-level-3-app-platform-migration.md) — the architectural resolution
- [`docs/DEPLOY.md`](../../../../docs/DEPLOY.md) — the pre-Level-3 manual deployment procedure (will be superseded by Phase 6)
- [`infra/terraform/environments/qa/README.md`](../qa/README.md) — sister env, fully automated
- [`infra/terraform/environments/preview/`](../preview/) — sister env, per-PR ephemeral via snapshot restore
