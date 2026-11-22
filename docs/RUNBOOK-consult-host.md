# Runbook — Grove Consult host (consult.gatheringatthegrove.com) — GOL-3577

**Owner:** DevOps (Terra) · **Spec:** grove-odoo-modules
`docs/superpowers/specs/2026-10-22-consult-dashboard-design.md` §D/§E, amended
by AgriforestryOS
`docs/superpowers/specs/2026-11-01-consult-platform-restructure-design.md` §C
(GOL-3722 / GOL-3723) · **App:** AgriforestryOS `apps/consult` · **Train:** #6
(freeze Fri 2026-11-27, promote Wed 12-02).

> **The app moved (2026-11-19).** `apps/consult` lives in **AgriforestryOS**,
> not grove-sites. Its image keeps the name
> `ghcr.io/goldberry-playground/grove-consult`, but the tags are now
> AgriforestryOS commit SHAs and prod pins them with its own variable,
> `consult_image_tag`. The storefront pins (`hub_image_tag` /
> `tenant_image_tag`) are grove-sites SHAs and no longer apply to consult.

The consult dashboard is an internal staff tool. It gets its own App Platform
app, its own hostname, its own Cloudflare Access policy and its own Spaces
prefix, so nothing about it can touch the four revenue frontends — and so it
can be switched off without touching them either.

## What lives where

| Piece | File | Deploy owner |
|---|---|---|
| `digitalocean_app.consult` (grove-consult-prod, GHCR `grove-consult:<sha>`) | `infra/terraform/environments/production/consult.tf` | **(a) promote-storefronts.yml** — targeted apply + `doctl create-deployment`. Pinned by `var.consult_image_tag` (an AgriforestryOS SHA), which moves only when the run is given the optional `consult_tag` input |
| `cloudflare_record.consult` (proxied CNAME → app ingress) | same file | **(c) Josh, manual targeted apply** — once, at first enable |
| `digitalocean_spaces_bucket_object.consult_bundles_readme` (`grove-odoo-backups/consult/README.md`) | same file | **(c) Josh, manual targeted apply** — once, any time |
| Cloudflare Access application + staff-email policy | **AgenticOS** `infra/terraform/cloudflare-access-consult.tf` | Josh, `terraform apply` in that root (its token has Access:Edit; this env's does not) |
| Source + image build: `apps/consult`, `.github/workflows/consult-image.yml` (build, `/healthz` smoke, Trivy, push `grove-consult:<sha>` + `latest` on main pushes that touch `apps/consult/**`) | **AgriforestryOS** | automatic on merge to main; publishing an image deploys nothing |
| `var.consult_enabled`, `consult_image_tag`, `consult_instance_size_slug`, `consult_access_team_domain`, `consult_access_aud` | `production/variables.tf` | — |

The three prod deploy owners are the ones `prod-plan-guard.yml` and
`promote-storefronts.yml` already describe: (a) the storefront promote, (b)
the Odoo modules pin (`prod-modules-promote.sh` / `reconcile-modules-pin.yml`),
(c) Josh's manual targeted applies for everything else. Consult adds no
fourth; the app is (a), the DNS record and README are (c).

## Moving the consult pin (`consult_tag`)

`consult_image_tag` is a 40-char **AgriforestryOS** SHA. It does not follow the
storefronts: a normal promote leaves it exactly where it is.

To ship a new consult build, run **Promote storefronts to production** with
the extra input:

| Input | Value |
|---|---|
| `target_sha` | as usual (blank = newest built grove-sites main commit) |
| `consult_tag` | the AgriforestryOS main SHA to pin — find it on the newest green **Consult image** run on main (`gh run list -R Goldberry-Playground/AgriforestryOS -w consult-image.yml -b main -s success -L 5`) |
| `confirm` | `PROMOTE` |

Before the approval gate the `prepare` job (`scripts/ci/consult-target.mjs`)
fails the run unless the SHA is a commit on AgriforestryOS `main` and
`grove-consult:<sha>` is on GHCR, and it prints the current → proposed consult
pin in the step summary. A grove-sites SHA is refused there (old
`grove-consult` tags from grove-sites still exist on GHCR; they are superseded
builds). A SHA behind the current pin is allowed with a warning — that is how
you roll consult back.

After approval the same run moves the pin, applies it, redeploys
`grove-consult-prod` last, and includes `consult_image_tag` in the reconcile
PR. To re-pin consult without changing the storefronts, pass the currently
pinned storefront SHA as `target_sha` (the storefronts get a no-op re-roll).

While `consult_enabled` is false the pin can still be moved this way (or by a
plain reviewed PR); nothing is deployed, the committed default just changes.

Dry run from a laptop (read-only):

```bash
INPUT_CONSULT_TAG=<sha> node scripts/ci/consult-target.mjs
```

## Gate: `consult_enabled` (default `false`)

Until the `grove-consult` image exists on GHCR the app cannot deploy, so the
app and its CNAME are `count`-gated off. The README object is not gated (the
prefix is harmless to settle early; `prod-plan-guard` will show that one
`+1` on this PR).

## First enable (Train #6)

Prerequisites, in order:

1. **A green `Consult image` run on AgriforestryOS main** that published
   `ghcr.io/goldberry-playground/grove-consult:<sha>` for the SHA you will pin.
   The committed default already points at the first one (`01c34e21`, the
   `apps/consult` move). The image must listen on `PORT=3001` and answer
   `GET /healthz` without an Access JWT: `consult.tf` sets `http_port = 3001`
   and health-checks `/healthz`, because the app 403s every other path —
   `/` included — when the `Cf-Access-Jwt-Assertion` header is missing.
2. **Access app applied in AgenticOS** (runbook there:
   `docs/runbooks/consult-cloudflare-access.md`) and its
   `terraform output -raw consult_access_aud` copied into
   `var.consult_access_aud` here (64-char hex, not a secret).

Then one PR in this repo: `consult_enabled = true` + the `consult_access_aud`
default (+ a `consult_image_tag` bump if AgriforestryOS main has a newer
consult build you want). Apply order after merge:

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
actually has it. Its pin moves only when the run is given `consult_tag` (see
above); otherwise that redeploy re-rolls the build already pinned.

### No QA twin

There is deliberately no `grove-consult-qa`: the QA tier's hosts live in the
DO-delegated `qa-l3` zone, which Cloudflare does not proxy, so an Access
policy cannot sit in front of a QA twin — a QA consult app would be an open
staff tool on the internet. The image's own CI (build, `/healthz` smoke,
fail-closed check, Trivy) and the app's Playwright flow against a fixture of
the Odoo routes stand in for it, per the spec.

## Verify

```bash
curl -sSI https://consult.gatheringatthegrove.com/ | head -1      # 302 → goldberrygrove.cloudflareaccess.com
doctl apps list -o json | jq '.[] | select(.spec.name=="grove-consult-prod") | .active_deployment.phase'  # ACTIVE
# the default ingress MUST deny without an Access JWT:
curl -s -o /dev/null -w '%{http_code}\n' "$(terraform output -raw consult_default_ingress)/"   # 401/403, never 200
curl -s -o /dev/null -w '%{http_code}\n' "$(terraform output -raw consult_default_ingress)/healthz"   # 200 (the only unauthenticated path)
# which build is live (must equal consult_image_tag):
doctl apps list -o json | jq -r '.[] | select(.spec.name=="grove-consult-prod") | .spec.services[0].image.tag'
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
