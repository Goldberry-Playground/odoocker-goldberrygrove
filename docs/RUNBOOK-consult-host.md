# Runbook — Grove Consult host (consult.gatheringatthegrove.com) — GOL-3577

**Owner:** DevOps (Terra) · **Spec:** grove-odoo-modules
`docs/superpowers/specs/2026-10-22-consult-dashboard-design.md` §D/§E ·
**App:** grove-sites `apps/consult` (Iris, GOL-3576) · **Train:** #6
(freeze Fri 2026-11-27, promote Wed 12-02).

The consult dashboard is an internal staff tool. It gets its own App Platform
app, its own hostname, its own Cloudflare Access policy and its own Spaces
prefix, so nothing about it can touch the four revenue frontends — and so it
can be switched off without touching them either.

## What lives where

| Piece | File | Deploy owner |
|---|---|---|
| `digitalocean_app.consult` (grove-consult-prod, GHCR `grove-consult:<sha>`) | `infra/terraform/environments/production/consult.tf` | **(a) promote-storefronts.yml** — targeted apply + `doctl create-deployment`, rides `var.tenant_image_tag` with the storefronts |
| `cloudflare_record.consult` (proxied CNAME → app ingress) | same file | **(c) Josh, manual targeted apply** — once, at first enable |
| `digitalocean_spaces_bucket_object.consult_bundles_readme` (`grove-odoo-backups/consult/README.md`) | same file | **(c) Josh, manual targeted apply** — once, any time |
| Cloudflare Access application + staff-email policy | **AgenticOS** `infra/terraform/cloudflare-access-consult.tf` | Josh, `terraform apply` in that root (its token has Access:Edit; this env's does not) |
| `var.consult_enabled`, `consult_instance_size_slug`, `consult_access_team_domain`, `consult_access_aud` | `production/variables.tf` | — |

The three prod deploy owners are the ones `prod-plan-guard.yml` and
`promote-storefronts.yml` already describe: (a) the storefront promote, (b)
the Odoo modules pin (`prod-modules-promote.sh` / `reconcile-modules-pin.yml`),
(c) Josh's manual targeted applies for everything else. Consult adds no
fourth; the app is (a), the DNS record and README are (c).

## Gate: `consult_enabled` (default `false`)

Until the `grove-consult` image exists on GHCR the app cannot deploy, so the
app and its CNAME are `count`-gated off. The README object is not gated (the
prefix is harmless to settle early; `prod-plan-guard` will show that one
`+1` on this PR).

## First enable (Train #6)

Prerequisites, in order:

1. **Iris's `apps/consult` PR merged in grove-sites** with (i) a Dockerfile
   that listens on `PORT=3001` like the nursery (or change `http_port` in
   `consult.tf` in the same train), (ii) a `consult` leg in
   `.github/workflows/docker.yml`'s matrix (snippet below), and (iii) the BFF
   verifying `Cf-Access-Jwt-Assertion` using `CF_ACCESS_TEAM_DOMAIN` +
   `CF_ACCESS_AUD`, failing closed when the AUD is unset.
2. A **Docker — Frontends** run on main that published
   `ghcr.io/goldberry-playground/grove-consult:<sha>` for the SHA you will pin.
3. **Access app applied in AgenticOS** (runbook there:
   `docs/runbooks/consult-cloudflare-access.md`) and its
   `terraform output -raw consult_access_aud` copied into
   `var.consult_access_aud` here (64-char hex, not a secret).

Then one PR in this repo: `consult_enabled = true` + the `consult_access_aud`
default (+ the `tenant_image_tag` bump if Train #6 hasn't moved it yet).
Apply order after merge:

```bash
cd infra/terraform/environments/production
terraform init -backend-config=backend.hcl
# (a)-shaped: the app, in place. No rebuild of anything else.
op run --env-file=.env.op -- terraform apply -target='digitalocean_app.consult'
# wait for the app's domain to reach phase=ACTIVE ("domain consult.gatheringatthegrove.com ready")
op run --env-file=.env.op -- terraform apply -target='cloudflare_record.consult' -target='digitalocean_spaces_bucket_object.consult_bundles_readme'
# the actual roll (a spec apply deploys nothing on GHCR apps -- GOL-1607 Lesson 1):
doctl apps create-deployment "$(doctl apps list -o json | jq -r '.[] | select(.spec.name=="grove-consult-prod") | .id')" --wait
```

From the next promote on, `promote-storefronts.yml` carries the consult app:
it is in the targeted apply and is redeployed last, only when App Platform
actually has it.

### grove-sites `docker.yml` matrix leg (for Iris's PR)

```yaml
          - app: consult
            internal_port: 3001
            image_name: ghcr.io/goldberry-playground/grove-consult
            qa_app: ""            # no QA App Platform twin (see below)
            smoke_routes: "/"
            env_extra: |
              -e ODOO_URL=http://127.0.0.1:1
              -e ODOO_API_KEY=smoke-test
              -e CF_ACCESS_TEAM_DOMAIN=goldberrygrove
```

The QA-redeploy step keys off `qa_app`; an empty value must skip it (one
`if: matrix.qa_app != ''` on the redeploy steps). There is deliberately no
`grove-consult-qa`: the QA tier's hosts live in the DO-delegated `qa-l3` zone,
which Cloudflare does not proxy, so an Access policy cannot sit in front of a
QA twin — a QA consult app would be an open staff tool on the internet. The D
Playwright flow runs against a fixture of the C routes until QA, per the spec.

## Verify

```bash
curl -sSI https://consult.gatheringatthegrove.com/ | head -1      # 302 → goldberrygrove.cloudflareaccess.com
doctl apps list -o json | jq '.[] | select(.spec.name=="grove-consult-prod") | .active_deployment.phase'  # ACTIVE
# the default ingress MUST deny without an Access JWT:
curl -s -o /dev/null -w '%{http_code}\n' "$(terraform output -raw consult_default_ingress)/"   # 401/403, never 200
```

## Kill switch

| Switch | Where | Effect | Nursery touched? |
|---|---|---|---|
| **Policy off** — delete/comment `cloudflare_zero_trust_access_policy.consult_allow_staff`, apply | AgenticOS root | everyone denied at the edge, app still running | no |
| **Component off** — `consult_enabled = false`, then `terraform apply -target='digitalocean_app.consult' -target='cloudflare_record.consult'` | this root | app + CNAME destroyed, host stops resolving | no — the targets name only the consult resources; `digitalocean_app.tenant["nursery"]` is a different resource and is not in the plan |

Fastest in an incident is the policy switch (one Cloudflare resource, no DO
rollout). Reversing the component switch needs the three-step first-enable
sequence again (app apply → wait ACTIVE → CNAME → create-deployment).

## Spaces prefix `consult/<opportunity>/` (phase 4 bundles)

Bucket `grove-odoo-backups` (private, `prevent_destroy`), written by the Odoo
droplet's bucket-scoped key `grove-odoo-backup` — no new credential.

```
consult/<opportunity>/<YYYY-MM-DD>-v<N>/
  plan.pdf  site-brief.pdf  plantings.geojson  plantings.kml  planting-points.csv
  <opportunity>.qgz  calendar.ics  pruning-guides.pdf  lots.csv  manifest.json
```

`<opportunity>` = `<crm.lead id>-<slugified lead name>` (e.g. `142-smith-hollow`),
the same key AgriforestryOS `scripts/new_client_project.py` names the
per-client QGIS project with.

**Retention: indefinite.** No lifecycle rule covers `consult/` (the bucket's
only rule expires `filestore/archive/` after 35 days). The bundle is the
client's archive-of-record after handover ("nothing is deleted"); removal is a
human console action, never a timer.
