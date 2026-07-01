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
        └─ POST OTLP metrics → OpenObserve
             synthetic_journey_success      gauge 1/0  {journey,tenant,tier=api,env}
             synthetic_journey_duration_ms  gauge ms   {...}
```

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

## Deferred to the next increment (need secrets / cleanup)

- **`ghost-content`** — Ghost Content API needs a per-tenant content key (secret plumbing).
- **`checkout-canary`** — the money path. Needs (a) a bearer API key, (b) the `$0 SYNTHETIC-CANARY` SKU + test partner seeded by `setup-monitoring.py`, and (c) an order-cancel mechanism (`/orders` has no cancel endpoint, so cleanup is XML-RPC). Built as its own PR so the order-creating loop is reviewed carefully.

## Run / debug

```bash
# Whole stack (runner comes up under the monitoring profile):
make monitoring-up

# One-off local run against an already-running stack, from this dir:
SYNTHETIC_ODOO_BASE=http://localhost:8069 \
OPENOBSERVE_OTLP_METRICS_URL=http://localhost:5080/api/default/v1/metrics \
OPENOBSERVE_ROOT_EMAIL=... OPENOBSERVE_ROOT_PASSWORD=... \
python3 run.py

# Unit-test the OTLP builder (no stack needed):
python3 test_run.py
```

> **Pending live validation:** the OTLP metrics endpoint path/auth (`/api/default/v1/metrics`, basic auth) is the documented OpenObserve ingest shape but hasn't been exercised end-to-end yet — confirm on first `make monitoring-up` and adjust `OPENOBSERVE_OTLP_METRICS_URL` if needed.
