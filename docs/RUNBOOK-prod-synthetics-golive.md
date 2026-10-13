# RUNBOOK — Tier-1 synthetics go-live vs PRODUCTION (GOL-2325)

**Owner:** DevOps (Terra) · **Parent EPIC:** GOL-2323 · **Spec:** `docs/specs/2026-06-26-grove-observability-design.md` §1 (Tier 1)

Brings up the supercronic + Hurl synthetic runner against **production**: six
journeys per tenant (`goldberry / ggg / nursery`) + shared, results → OTLP
metrics into the existing obs-droplet OpenObserve. This is the primary
"is the site up" signal and gates child 2 (Keep → Discord alerting).

Everything here is config-as-code (`docker-compose.monitoring.app-plane.yml`,
`synthetic/`, `scripts/setup-monitoring.py`, `scripts/prod-monitoring.env.op`,
Makefile `monitoring-*` targets). No click-ops; this runbook only sequences the
gated human steps that code cannot self-serve (minting a prod secret, CEO
approval to write prod data).

---

## 0. Blocking gates

**Gate (c) is the one that actually decides whether 2026-10-10 is met. It is not
about the money path — nothing observability ships from prod reaches the sink
until it clears.**

| Gate | Owner | Blocks | What it unblocks |
|---|---|---|---|
| **(c)** `grove-obs-fw` must admit grove-prod-odoo on **:5080** | Josh (apply) | **ALL prod ingest** — synthetics *and* Beyla (GOL-2332) | Any metric from prod reaching OpenObserve at all |
| **(a)** CEO OK to seed a `$0 SYNTHETIC-CANARY` product + test partner into **prod** Odoo | CEO | checkout-canary journey only | `SYNTHETIC_CANARY_ENABLED=true` + `monitoring-setup` seeding prod |
| **(b)** Least-privilege canary Odoo user + API key minted in 1Password | Josh | checkout-canary journey only | the two commented `ODOO_LOGIN` / `SYNTHETIC_ODOO_API_KEY` refs |

### Gate (c) — verified live 2026-09-29, currently FAILING

`grove-obs-fw`'s inbound :5080 rule admits exactly three `/32`s and **no
`source_tags`**:

| Source on :5080 | What it is |
|---|---|
| `159.223.171.231/32` | agenticos droplet |
| `167.71.109.184/32` | **stale** — grove-qa-l3-odoo's pre-2026-09-08-rebuild IP, not our droplet (ADR-010) |
| `74.47.41.38/32` | **stale** admin IP (Josh's rotated `173.84.140.152/32` is absent) |

grove-prod-odoo is `174.138.119.171` (public) / `10.132.23.194` (private) and is
in **none** of them, so every OTLP ship from prod is refused. DO cloud firewalls
filter the VPC interface too, so using the private address does not route around
this. ADR-010 decision #2 already fixes it by admitting app-plane collectors via
the `role-odoo` **droplet tag** (prod-odoo carries that tag) — but that decision
lives only in `variables.tf` defaults and **has not been applied**. Tag matching
also survives droplet replaces, which matters: ADR-010 records prod-odoo as
`138.197.44.60`, an IP it no longer has.

Apply it per ADR-010 (firewall-only, cannot replace the droplet), from the
operator checkout holding the observability tfvars — **first delete the
`admin_ip_cidrs` / `ingest_source_cidrs` / `automation_ssh_cidrs` lines from that
tfvars** or they override the new defaults:

```sh
terraform -chdir=infra/terraform/environments/observability plan  -target=digitalocean_firewall.obs
terraform -chdir=infra/terraform/environments/observability apply -target=digitalocean_firewall.obs
```

Expect `1 to change, 0 to destroy` on `grove-obs-fw`. Confirm afterwards that the
:5080 rule carries `source_tags` and that the stale `167.71.109.184/32` is gone.
Drift on this firewall is tracked by GOL-2564.

### Gates (a)/(b) — read-only tier ships without them

With `SYNTHETIC_CANARY_ENABLED=false` (the committed default) the runner brings
up **health / catalog / cart-flow / ghost-content** plus all the HTTP/TCP/SSL
probes. `canary.py` fail-closes (no key ⇒ no-op) and only checkout-canary is
skipped. That read-only set is already enough to satisfy the EPIC's "four
storefronts + prod Odoo observably up" deliverable, so **the 10-10 deadline does
not depend on the CEO seed decision** — only the money-path journey does.

Note gate (b) is **half done already**: the three per-tenant Ghost read-only
Content API keys it asks for are ALREADY in the vault, as one JSON object at
`Grove Infra/ghost_content_keys_tf_json` (verified 2026-09-29). `run.py` reads
that bundle directly, so no human step remains for Ghost. Only the canary Odoo
key is still human-only.

Cross-ref: the obs-home question the EPIC raised is settled — ADR-010 is
accepted and grove-obs is the canonical sink, so there is no "stays QA-l3"
ratification left to wait on here.

---

## 1. Mint the least-privilege canary Odoo user (Josh, gate b)

The canary API key doubles as the `/orders` HTTP bearer **and** the XML-RPC
password. Scope it as tight as the money path allows:

1. In prod Odoo, create a dedicated user `synthetic-canary` (login e.g.
   `synthetic-canary@grove.internal`).
2. Grant only what the three canary ops need:
   - create/write `product.template` + `product.product` (seed),
   - create `sale.order` + `unlink` on `sale.order` (checkout + sweep).
   No accounting, no payments, no admin/settings.
3. Generate an **API key** for that user (Preferences → Account Security → New
   API Key). This is the secret; the login is not.
4. Store both in 1Password (vault `Grove Prod`), then update the two
   `REPLACE_ITEM_ID` refs in `scripts/prod-monitoring.env.op`:
   - `synthetic_canary_odoo_login`
   - `synthetic_canary_odoo_api_key`
5. ~~Also store the three per-tenant Ghost read-only Content API keys.~~
   **Not needed** — they are already in the vault as the
   `Grove Infra/ghost_content_keys_tf_json` bundle, which `run.py` reads
   directly (verified 2026-09-29, GOL-2325).

The op service account is READ-ONLY — it can read these once stored but cannot
create them, so this step is human-only.

---

## 2. Build the runner image on the prod Odoo droplet

The app-plane compose runs on the prod Odoo droplet and references
`grove-synthetic-runner:local` (built locally, not pulled). Sync the repo dir to
`/etc/grove` (same place the compose + otel configs live), then build:

```sh
cd /etc/grove
docker build -t grove-synthetic-runner:local ./synthetic
```

Deliberate version pins live in `synthetic/Dockerfile` (Hurl 5.0.1, supercronic
v0.2.33). The image is python-slim — `run.py` needs no third-party deps.

---

## 3. Render the deploy env-file from 1Password + bring the stack up

`prod-monitoring.env.op` holds op:// refs + non-secret constants; `op run`
resolves them into `/etc/grove/.env.monitoring`. `make monitoring-up` depends on
`make monitoring-setup`, so the canary is **seeded before the runner ever
fires** (spec §1 invariant).

```sh
cd /etc/grove
op run --env-file=scripts/prod-monitoring.env.op -- \
  sh -c 'env | grep -E "^(OPENOBSERVE|KEEP|DISCORD|SYNTHETIC|ODOO|GHOST|POSTGRES|COST|OTELCOL|BEYLA)_" \
         > /etc/grove/.env.monitoring'
chmod 600 /etc/grove/.env.monitoring

# Seed the $0 canary + upload monitors/alerts (idempotent), then start the runner.
make monitoring-up \
  MONITORING_COMPOSE=docker-compose.monitoring.app-plane.yml \
  MONITORING_ENV_FILE=/etc/grove/.env.monitoring
```

`monitoring-setup` runs `setup-monitoring.py`, which (with
`SYNTHETIC_CANARY_ENABLED=true`) invokes `canary.py --seed` to upsert the
unpublished `$0` product per company before any journey runs.

---

## 4. Verify the success condition (spec §1)

**4a. All six journeys per tenant emit pass/fail + latency series.**
In OpenObserve (grove-obs), query the `synthetic_journey_success` and
`synthetic_journey_duration_ms` metric streams. Expect data points for:
`health` (tenant=shared), and per tenant `catalog`, `cart-flow`,
`checkout-canary`, `ghost-content` — tagged `env=prod`, `tier=api`. (catalog
covers both products-list and product-detail in one journey.)

```sh
docker logs grove-monitoring-synthetic-runner-1 --tail 40
# expect e.g.: "N/N journeys passed" then "shipped N results → OpenObserve (HTTP 200)"
```

**4b. A deliberately-broken URL flips the signal red.**
Temporarily point one journey's base at a bad host (e.g. set
`SYNTHETIC_ODOO_BASE=http://odoo:9999` in a throwaway env and run
`python3 /app/run.py` once inside the container) and confirm
`synthetic_journey_success` drops to `0` for the affected journeys, then revert.

**4c. Canary orders auto-cancel — prod books unchanged.**
`checkout-canary.hurl` posts a `$0` order then `run.py` sweeps the draft via
`canary.cleanup_orders()` each cycle. Verify prod Odoo confirmed-order count is
**unchanged** (cross-ref GOL-1795: prod = 0 real orders ever):

```sh
# on the droplet, via the canary user's XML-RPC (read):
#   sale.order search_count [] state in (sale, done)  → must stay 0
docker exec grove-monitoring-synthetic-runner-1 python3 -c \
  "import canary,os; c=canary._client(); \
   print('confirmed:', c.call('sale.order','search_count',[[['state','in',['sale','done']]]])); \
   print('canary drafts:', c.call('sale.order','search_count',[[['partner_id.email','=',canary.CANARY_EMAIL]]]))"
```

Confirmed orders must read `0`; canary drafts should trend to `0` after a sweep
(any residual are swept next cycle — cleanup is self-healing).

**4d. (After child 2)** confirm a sustained red journey routes to Discord via
Keep. Out of scope for this leg; this runbook only proves the signal exists.

---

## 5. Rollback

The overlay is independently deployable and stateless — nothing here touches
Odoo's DB except the `$0` canary product + its own draft orders.

```sh
# stop the runner (and app-plane otel/beyla) — leaves OpenObserve untouched
make monitoring-down \
  MONITORING_COMPOSE=docker-compose.monitoring.app-plane.yml \
  MONITORING_ENV_FILE=/etc/grove/.env.monitoring
```

To fully back out the prod-data footprint (only if the CEO reverses gate a):
archive the `SYNTHETIC-CANARY` product (`active=false`) and unlink any residual
canary drafts — `canary.cleanup_orders()` already removes the drafts; the
product is unpublished + `$0` so it is invisible to storefronts regardless.

No Terraform/DNS/TLS state is involved; there is nothing to `terraform destroy`.

---

## 6. Config-as-code inventory (what this runbook orchestrates)

| Artifact | Role |
|---|---|
| `synthetic/journeys/*.hurl` | the six Tier-1 journeys |
| `synthetic/run.py` | orchestrator → OTLP metrics shipper |
| `synthetic/canary.py` | XML-RPC seed / resolve / cleanup (fail-closed) |
| `synthetic/Dockerfile` + `crontab` | supercronic + Hurl runner image |
| `scripts/setup-monitoring.py` | idempotent seed + monitors/alerts upload |
| `scripts/prod-monitoring.env.op` | 1Password-referenced prod env contract |
| `docker-compose.monitoring.app-plane.yml` | app-plane overlay (runner + otel + beyla) |
| `Makefile` `monitoring-{setup,up,down}` | bring-up (setup gates up) |
