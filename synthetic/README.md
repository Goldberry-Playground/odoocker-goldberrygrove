# Grove synthetic Tier-1 (Hurl)

Continuous, external **API journeys** against the live `grove_headless` storefront
API — the Tier-1 half of the synthetic-tests design (`docs/specs/2026-06-26-grove-observability-design.md` §1). Runs as a container in the monitoring stack; results land in OpenObserve as metrics that the synthetic alert rules query.

## How it works

```
supercronic (crontab, every 60s)
   └─ run.py
        ├─ hurl --test journeys/health.hurl        (shared)
        ├─ hurl --test journeys/catalog.hurl       (per tenant, X-Grove-Tenant)
        ├─ hurl --test journeys/cart-flow.hurl     (per tenant)
        ├─ POST OTLP metrics → OpenObserve
        │    synthetic_journey_success      gauge 1/0  {journey,tenant,tier=api,env}
        │    synthetic_journey_duration_ms  gauge ms   {...}
        └─ probes.emit()  (SYNTHETIC_UPTIME_ENABLED, default on)
             synthetic_uptime               gauge 1/0  {target,tenant,tier,service,route,env,success}
             synthetic_ssl_days             gauge days {target,host,tenant,env,days_until_expiry}
```

## Availability + SSL probes (`probes.py`) — GOL-280

Tier-1 also runs the HTTP/TCP/SSL **availability** targets in
`openobserve/monitors.json` (storefront roots/shop/blog, hub, Odoo, Ghost admin,
Postgres TCP, the four SSL certs) — OpenObserve v0.91.1 has **no `/synthetic`
endpoint**, so the runner performs these probes itself and ships two streams the
availability alerts fire on:

- `synthetic_uptime` — 1/0 per target. The `frontend-down` / `odoo-down` /
  `ghost-down` / `postgres-down` alerts filter `success=0`.
- `synthetic_ssl_days` — days until cert expiry per host. `ssl-expiring` filters
  `days_until_expiry<14`.

`monitors.json` is the single source of truth for targets (add/remove a probe =
config, not code). Local topology (`host.docker.internal:<port>`, service-name
DNS) is the file default; override per target per-env without editing the file:
`SYNTHETIC_MONITOR_URL_<NAME>` / `SYNTHETIC_MONITOR_HOST_<NAME>` (NAME = monitor
name upper-cased, `-`→`_`). ON by default; set `SYNTHETIC_UPTIME_ENABLED=false`
where the targets aren't reachable (a laptop with no storefront containers) so it
doesn't emit false-down. `success` / `days_until_expiry` are emitted as explicit
numeric OTLP attributes (not only the bare gauge value) so the alert column
filters resolve — confirm the exact field names on first real traffic (GOL-280).

- **Pass/fail** = the Hurl `--test` exit code. **Duration** = wall-clock of the run.
- `run.py` always exits 0 — a failing journey is reported as `success=0`, not as a crashed cron job.
- Tenant scoping is the `X-Grove-Tenant` header; journeys reach Odoo by service-name DNS (`http://odoo:8069`) on the shared `gatheratthegrove_internal` network.
- Journeys are DB-agnostic: `catalog`/`cart-flow` capture a product id from the live list rather than hard-coding one.

## Journeys (this slice)

| Journey | Scope | Asserts | Side effects |
|---|---|---|---|
| `health` | shared | `/health` → 200, `status==ok` | none |
| `catalog` | per tenant | products list non-empty → detail has a variant price | none (reads) |
| `cart-flow` | per tenant | add line → cart reflects it | a draft cart sale.order/run (Odoo expires these) |
| `checkout-canary` | per tenant (opt-in) | create $0 order via bearer `/orders` → access_token gate (200 w/ token, 403 w/o) | a draft order/run, swept by XML-RPC cleanup |
| `ghost-content` | per tenant (opt-in) | Ghost Content API v5 returns ≥1 published post | none (reads) |

## checkout-canary (the money path) — opt-in

OFF unless `SYNTHETIC_CANARY_ENABLED=true` + `ODOO_DB`/`ODOO_LOGIN`/`SYNTHETIC_ODOO_API_KEY` are set. It's the highest-value journey (proves orders can be created + the PII gate holds) but the only one that writes, so it's gated. Design (`canary.py` owns the XML-RPC):

- **Shared $0 product.** `setup-monitoring.py` seeds one `SYNTHETIC-CANARY` product with `company_id=False` (visible to all tenants) and `website_published=False` (never in a storefront). One variant serves every tenant — no tenant→company mapping.
- **Self-healing cleanup.** After each cycle the runner unlinks draft/sent orders whose partner email is `synthetic-canary@grove.invalid` — never confirmed orders, and `.invalid` can't collide with a real customer.
- **Key handling.** The API key doubles as HTTP bearer + XML-RPC password; it's passed to Hurl via a temp variables-file (out of argv). Scope it to a least-privilege Odoo user.

## ghost-content — opt-in

OFF unless `SYNTHETIC_GHOST_ENABLED=true` + per-tenant `GHOST_URL_<TENANT>` / `GHOST_KEY_<TENANT>` (read-only Content API keys). Checks each tenant's blog has published posts — content-level depth beyond the `ghost-*-admin` availability monitors, catching a rotated/invalid key or empty blog that would break `/blog` even though Ghost is "up". Missing a tenant's pair just skips that tenant; the key is passed to Hurl via a variables-file (out of argv).

**Synthetic Tier-1 is now complete** — all six journeys (health, catalog, cart-flow, checkout-canary, ghost-content) are implemented.

## Run / debug

```bash
# Whole stack (runner comes up under the monitoring profile):
make monitoring-up

# One-off local run against an already-running stack, from this dir:
SYNTHETIC_ODOO_BASE=http://localhost:8069 \
OPENOBSERVE_OTLP_METRICS_URL=http://localhost:5080/api/default/v1/metrics \
OPENOBSERVE_ROOT_EMAIL=... OPENOBSERVE_ROOT_PASSWORD=... \
python3 run.py

# Unit-test the OTLP builders (no stack needed):
python3 test_run.py && python3 test_probes.py

# One-off probe run (SSL works anywhere with internet; HTTP/TCP need the targets):
OPENOBSERVE_OTLP_METRICS_URL=http://localhost:5080/api/default/v1/metrics \
OPENOBSERVE_ROOT_EMAIL=... OPENOBSERVE_ROOT_PASSWORD=... python3 probes.py
```

> **Pending live validation:** the OTLP metrics endpoint path/auth (`/api/default/v1/metrics`, basic auth) is the documented OpenObserve ingest shape but hasn't been exercised end-to-end yet — confirm on first `make monitoring-up` and adjust `OPENOBSERVE_OTLP_METRICS_URL` if needed.
