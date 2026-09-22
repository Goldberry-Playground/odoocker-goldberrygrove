# ADR 010: grove-obs is the canonical observability droplet; grove-qa-l3-obs is QA-only

**Status:** Proposed. Needs CEO ratification because the placement decision can't easily be undone (GOL-2333).
**Date:** 2026-09-22
**Deciders:** CEO (ratify), Josh Dunbar (apply)
**Relates to:** [ADR-007](./007-level-3-app-platform-migration.md) (D1 "same shape"), [ADR-008](./008-observability-openobserve-supersedes-adr004.md), EPIC GOL-2323, GOL-1844 (admin allowlist), `docs/specs/2026-06-26-grove-observability-design.md` §4

## Context

GOL-2333 asked where the OpenObserve+Keep sink should live. The two options were to keep it on "the QA-l3 obs droplet" or to move it to a dedicated prod-adjacent obs droplet. The EPIC also said "the obs droplet" is exempt from the release-train teardown until this is decided.

A live inventory taken on 2026-09-22 (DO API, read-only) showed that the question rests on a stale premise. **Two** obs droplets exist, and the dedicated one already runs:

| Droplet | TF env / state | Size | Consumers | In release-train teardown? |
|---|---|---|---|---|
| **grove-obs** `159.65.46.198` | `environments/observability/` with its own state (`observability/terraform.tfstate`) | s-2vcpu-4gb | **All of Phase 2:** app-plane collector + Beyla (#673), prod synthetics (#675), CF-WAF OTLP ingest (#691, 443 locked to CF IPs, live), RUM, Discord bridge, agenticos collector (GOL-54) | **No.** `qa-l3-teardown.sh` only runs against `qa-app-platform` state. |
| **grove-qa-l3-obs** `162.243.187.209` | `environments/qa-app-platform/observability.tf` (QA state) | s-1vcpu-2gb | None found in repo config or runbooks. It was the Phase-1.5 QA stack behind `oo.qa.*` / `keep.qa.*`. | **Yes.** It was a `compute` target (`digitalocean_droplet.obs`). |

So the "dedicated prod-adjacent obs droplet (~$12–24/mo, spec §9)" option already exists. It is grove-obs, which has been applied since 2026-07-11 (GOL-270). It is a separate failure domain from the app plane, it has its own Terraform state, and teardown can't reach it.

## Decision

1. **grove-obs (`environments/observability/`) is the canonical home** for the OpenObserve+Keep sink, for the 2026-10-10 deadline and afterwards. Phase-2 children keep shipping against it. No move or replica is needed, and no new droplet is provisioned.
2. **The network posture of grove-obs is codified** (this closes the GOL-1844 fold-in). `admin_ip_cidrs`, `ingest_source_cidrs` and `automation_ssh_cidrs` are now `variables.tf` defaults, not operator-local tfvars values. App-plane collectors are admitted to 5080 by **droplet tag** (`role-odoo`, `env-qa-l3`), not by /32. The live firewall had drifted in three ways:
   - The admin allowlist had only the stale `74.47.41.38/32`. Josh's rotated `173.84.140.152/32` was missing, although prod and QA already carry it.
   - 5080 still admitted `167.71.109.184/32`. That was grove-qa-l3-odoo's IP before its 2026-09-08 rebuild and is no longer our droplet.
   - Because of that, the QA app-plane collector's current IP has been **blocked** from ingest since that rebuild. Prod-odoo (`138.197.44.60`) was never admitted. Tag matching makes this survive rebuilds.
3. **grove-qa-l3-obs is QA-only and not load-bearing for monitoring.** Until the CEO ratifies this ADR, the teardown exemption is enforced in code: `qa-l3-teardown.sh compute` no longer destroys it unless `QA_L3_TEARDOWN_OBS=1`. **Recommendation:** on ratification, lift the exemption (set the flag in the train-teardown, or flip the default). The better option is to retire the droplet entirely in a follow-up PR that deletes `qa-app-platform/observability.tf`. That saves about $12/mo and removes a second, unmonitored copy of the stack.

## Consequences

- The prod-shape replica asked for in ADR-007 D1 is already in place (grove-obs). Monitoring outlives an app-plane outage because grove-obs sits on separate state and a separate droplet.
- **Josh-gated apply (firewall only, in place):** run from the operator checkout that holds the observability `terraform.tfvars`. **First delete the `admin_ip_cidrs`, `ingest_source_cidrs` and `automation_ssh_cidrs` lines from that tfvars.** Otherwise they override the new defaults. Then run:
  ```
  terraform -chdir=infra/terraform/environments/observability plan  -target=digitalocean_firewall.obs
  terraform -chdir=infra/terraform/environments/observability apply -target=digitalocean_firewall.obs
  ```
  The expected plan is `1 to change, 0 to destroy` on `grove-obs-fw`. These variables feed only firewall `source_addresses`/`source_tags` and never `user_data`, so the droplet can't be replaced. `-target` keeps any unrelated pending `user_data` drift out of this apply.
- **Rollback:** restore the three lines in tfvars and re-apply the same target.
- Future operator-IP rotations follow `docs/RUNBOOK-refresh-admin-ip-cidrs.md`, which now covers three envs.
