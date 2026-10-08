# ADR 011: Terraform state locking must move off DO Spaces — choosing the replacement backend

**Status:** Proposed. Needs a board decision (this is a one-way-ish door: the migration rewrites production state). Recorded 2026-09-30 by DevOps - Terra under GOL-2755.
**Date:** 2026-09-30
**Deciders:** CEO (choose option), Josh Dunbar (apply the migration), DevOps - Terra (implement + verify)
**Relates to:** GOL-2755 (this decision), GOL-2584 (Train #2 prerequisite check that found it), PR #790 (the advisory stop-gap), [ADR-009 grove asset storage](./009-grove-asset-storage.md), EPIC GOL-2324 (release train) — **must not land inside the Train #2 window (2026-10-05 … 10-08)**

## Context

### The control we thought we had does not exist

Every Grove Terraform environment sets `use_lockfile = true` in its `backend "s3"` block, and every one of those blocks carries the comment *"Verified DO Spaces enforces it (2nd writer gets HTTP 412)"*. That comment is false.

Terraform's S3-native locking is built entirely on the object store **rejecting** a conditional `PUT <key>.tflock` carrying `If-None-Match: *` once the object exists. Measured against `grove-tf-state` (nyc3) on 2026-09-30:

```
PUT _locktest/probe   If-None-Match: *   -> 200
PUT _locktest/probe   If-None-Match: *   -> 200     (S3 would answer 412)
GET _locktest/probe                      -> "second"  (silently overwritten)
```

Two concurrent `terraform plan -lock-timeout=0` runs against `cloudflare-policy` both ran to completion. Neither printed `Error acquiring the state lock`. The first to finish deleted the shared lockfile, so the second exited 1 on `Error releasing the state lock … StatusCode: 404` — **the exact signature of the 2026-09-29 double teardown.**

So the blast radius is not theoretical and it is not confined to one env: `use_lockfile` is a no-op in **all** of them, production included. Regression test: `op run --env-file=infra/terraform/environments/<env>/.env.op -- bash scripts/tf-state-lock-check.sh probe`.

PR #790 adds an advisory `guard` preflight to both release-train legs. It closes the realistic window — a second operator starting minutes into someone else's run — but it cannot close a sub-second race, because the atomic compare-and-set it would need is precisely what Spaces lacks. **Mitigated, not fixed.**

### How big is the thing we would be migrating

Live read-only inventory, 2026-09-30, via `scripts/tf-state-inventory.sh` (added by this change):

| state key | managed | serial | last written by |
|---|---:|---:|---|
| `production/terraform.tfstate` | **60** | 58 | 1.15.6 |
| `qa-app-platform/terraform.tfstate` | 13 | 99 | 1.15.6 |
| `cloudflare-edge/terraform.tfstate` | 8 | 14 | 1.10.5 |
| `assets/terraform.tfstate` | 7 | 9 | 1.15.6 |
| `cloudflare-policy/terraform.tfstate` | 4 | 3 | 1.10.5 |
| `observability/terraform.tfstate` | 4 | 10 | 1.10.5 |
| `infisical-identities`, `qa`, `preview/pr-{67,106,108,124,125}` | 0 | — | — |

**96 managed resource instances; only 6 of the 13 state objects hold anything.** The issue text estimated "a state migration per env (9 envs)" — the real number is **6 live migrations**, and only one of them (production, 60 resources, ~197 KB) is revenue-bearing. That materially cheapens every option that requires a migration. Zero stale `.tflock` objects were present at the time of inventory.

### Two constraints that eliminate the obvious answer

**(a) Terraform manages the Postgres clusters it would be asked to lock against.** `digitalocean_database_cluster.pg` lives in `production/terraform.tfstate` and again in `qa-app-platform/terraform.tfstate`. Putting state inside a cluster that the same state manages is a bootstrap cycle: a destroy, a replace, or a failed apply on that resource takes the state with it.

**(b) Neither cluster is reachable from CI.** Live DO API read, 2026-09-30:

| cluster | trusted sources |
|---|---|
| `grove-prod-pg` | `173.84.140.152`, `74.47.41.38`, droplet `601081550` (grove-prod-odoo) |
| `grove-qa-l3-pg` | `173.84.140.152`, `74.47.41.38` |

Confirmed by probe: TCP `:25060` to both clusters **times out** from the agent plane (`159.223.171.231`). GitHub Actions hosted runners are not admitted either, and they run `terraform plan` today (`ci.yml`, `prod-plan-guard.yml`). `backend "pg"` on either existing cluster therefore forces one of: admit GitHub's entire shared-runner fleet to the database that holds **production Odoo customer and order data**; move all plans onto a self-hosted runner inside the VPC; or drop the firewall to password-only — which is the posture GOL-2582 already flagged as a defect.

**(c) State is secret-bearing, which constrains where it may live.** `production/terraform.tfstate` carries 18 Terraform-marked sensitive attributes, including `digitalocean_database_cluster.password` / `uri`, `digitalocean_database_user.password` + `access_cert`, `digitalocean_spaces_key.secret_key`, `tls_cert_request.private_key_pem`, the Discord alert webhook URLs, and every droplet's `user_data`. Any option that parks state with a third party parks those with them too. Terraform (unlike OpenTofu) has no client-side state encryption.

## Options

| # | Option | Real mutual exclusion | Secrets stay ours | Works from GH Actions | Recurring cost | Migration |
|---|---|---|---|---|---|---|
| 1a | `backend "pg"` on an **existing** cluster | yes | yes | **no** (b) — and cycles (a) | $0 | 6 states |
| 1b | `backend "pg"` on a **new, state-only** cluster owned by `state-backend` | yes | yes | only if its firewall opens to runners | ~$15/mo | 6 states + 1 cluster |
| 2 | Wait for DO to ship conditional writes | **no** | yes | n/a | $0 | none |
| 3 | Keep PR #790's advisory guard only | **no** (sub-second race) | yes | yes | $0 | none |
| 4 | DynamoDB lock table | yes | yes | yes | ~$0 | none (backend arg only) — but HashiCorp has marked `dynamodb_table` **deprecated, "will be removed in a future minor version"**, so this buys a second migration later plus an AWS account |
| 5 | **Re-home the bucket to Cloudflare R2**, keep `backend "s3"` + `use_lockfile` | **unverified — must probe** | yes | yes | ~$0 at 13 objects / 200 KB | copy objects + `init -reconfigure`; no `-migrate-state` per env |
| 6 | HCP Terraform free tier | yes | **no** — ships (c) to a SaaS | yes | $0 (96 « 500 resource cap) | 6 states |

## Recommendation

**Probe option 5 first; fall back to 1b.**

Option 5 is the only candidate that fixes the actual defect without adding a database, a firewall hole, an AWS account, or a third-party custodian for our secrets. We already run Cloudflare for every zone, so it adds no new vendor. The config delta is an endpoint and a key pair — `use_lockfile` and the `<env>/<name>.tfstate` key layout are unchanged — and at 13 objects totalling under a megabyte the storage is free in practice. Cloudflare's S3-compatibility matrix lists `If-None-Match` as supported on `PutObject`.

**It must be measured, not believed.** A vendor doc listing the header as "supported" is exactly the evidence that produced the false `use_lockfile` comment we are now unwinding. Adoption is gated on running the GOL-2584 probe against an R2 bucket and seeing a real **412** on the second conditional PUT, plus two concurrent `terraform plan -lock-timeout=0` where the second prints `Error acquiring the state lock`. If R2 answers 200 like Spaces does, option 5 is dead on the spot and we take 1b.

Option 1b, the fallback, is sound but strictly more expensive: a new ~$15/mo cluster, owned by `state-backend` so the cycle in (a) does not reappear, with its own firewall to argue about for CI.

Option 3 alone is not acceptable as an endpoint. It is the correct posture *today* and through the Train #2 window, but it leaves a live corruption path on production state.

## Decision

**Pending.** The board picks on GOL-2755 — pending `ask_user_questions` interaction `ask:GOL-2755:state-lock-backend` (options: probe R2 / go straight to state-only PG / advisory guard only / HCP).

## Consequences (of the recommended path)

- **Nothing lands inside the Train #2 window (2026-10-05 … 10-08).** PR #790's advisory guard is the control for that window. This ADR's migration is scheduled after teardown at the earliest.
- **Prerequisite from Josh (blocking the probe):** a Cloudflare **account-scoped** API token with `Workers R2 Storage: Edit`, vaulted as `op://Goldberry Grove - Admin/Grove Infra/r2_*`. The zone-scoped token we hold today cannot create a bucket. The probe itself costs minutes once the token exists.
- **Migration shape (option 5), per state, 6 times:** `tf-state-inventory.sh --json > before.json` → server-side copy of the object to R2 → `terraform init -reconfigure -backend-config=backend.hcl` → `terraform plan` must come back **no changes** → `tf-state-inventory.sh --compare before.json` must exit **0**. Production goes last, behind a fresh backup of the Spaces object.
- **Count parity is not the gate; the fingerprint is.** `--compare` fails (exit 4) on a changed `lineage` — the signature of an `init` that started a *fresh* state instead of migrating one, which count parity cannot see — on a serial that went backwards, on a state key that vanished, and on any change to the sorted set of managed resource addresses (a lost resource offset by a gained one nets to the same count and is otherwise invisible until the next apply proposes a create for something that already exists). All four detections were exercised against the live bucket on 2026-09-30.
- **Pre-migration baseline, captured 2026-09-30 (`--json`, read-only).** Rollback reference; the migration is only correct if these survive it unchanged:

  | state key | lineage | managed | sha256(addresses) |
  |---|---|---:|---|
  | `production/terraform.tfstate` | `4faed703…` | 60 | `3665571af21b…` |
  | `qa-app-platform/terraform.tfstate` | `ea0ab4ce…` | 13 | `312310528540…` |
  | `cloudflare-edge/terraform.tfstate` | `a5a465fb…` | 8 | `9a9d369d25d9…` |
  | `assets/terraform.tfstate` | `5b3fe35c…` | 7 | `fbd2ed759b93…` |
  | `cloudflare-policy/terraform.tfstate` | `1f2c42b3…` | 4 | `058a6fd73c03…` |
  | `observability/terraform.tfstate` | `a97fd0a9…` | 4 | `d2456deed4e3…` |
- **Rollback is cheap and total** while the Spaces objects are left in place: revert `backend.hcl` to the Spaces endpoint and `init -reconfigure`. Do not delete anything from `grove-tf-state` until every env has run clean for one full train cycle.
- **`scripts/tf-state-lock-check.sh probe` retires** the day the replacement backend answers 412 — that is its stated exit condition. `scripts/tf-state-inventory.sh` does not retire; `--compare` is the before/after parity gate for this migration and the table mode stays the standing stale-lock detector.
- **Version drift surfaced by the inventory, tracked separately:** states are written by both 1.10.5 and 1.15.6. Terraform refuses to write a state last touched by a newer version, so whichever host performs the migration must be on 1.15.6 or later for all six.
