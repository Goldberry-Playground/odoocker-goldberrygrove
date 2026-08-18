# Runbook — Production Storefront Release (App Platform)

**Owner:** DevOps (Terra) · **Exercisable by:** any operator with `doctl` DO auth
(non-Josh operable) · **Related:** GOL-1325 (release-flow exercise), GOL-1607
(stale-storefront incident), GOL-1304 (pin-SHA deploy policy), GOL-1600 (codify).

> **Why this runbook exists.** `docs/RELEASE.md` + `.github/workflows/release.yml`
> deploy **only the Odoo compose stack** (SSH → `git checkout <tag>` →
> `docker compose pull/up`). They **do not roll the four revenue storefronts**,
> which run as **DigitalOcean App Platform** apps pulling **GHCR** images tagged
> `latest`. The release workflow's post-deploy smoke *pings* the storefront URLs
> but nothing in that pipeline ever *deploys* a new storefront build. Cutting a
> `vX.Y.Z` tag today updates Odoo and leaves all four storefronts frozen. This
> runbook is the missing half.

---

## The load-bearing invariants (read before you touch anything)

1. **`deploy_on_push` is DOCR-only — it NEVER fires for our GHCR apps.** The spec
   has `deploy_on_push: {enabled: true}`, but App Platform only auto-deploys on
   pushes to a **DigitalOcean Container Registry** repo. Our images live in
   **GitHub Container Registry (ghcr.io)**, so nothing auto-deploys. Apps re-pull
   `latest` **only** when a deployment is explicitly triggered. (Root cause of the
   GOL-1607 incident: 3 of 4 prod storefronts silently drifted to a stale build.)

2. **A green `terraform apply` is NOT evidence of a deploy.** Terraform updating
   image-tag vars / app spec does not, by itself, roll a new build onto the app.
   You MUST issue an explicit `doctl apps create-deployment` and prove a build
   actually rolled (fingerprint change or fingerprint-matches-QA, below).

3. **NEVER issue two `create-deployment` calls back-to-back on one app.** DO treats
   the first (now-superseded) deploy as **failed** and auto-fires an *"automated
   rollback after failed deployment"* that wins the race and **restores the prior
   (stale) build** → the app goes ACTIVE serving the old code. Fire **exactly one**
   deployment per app, then **poll to a terminal phase** before doing anything else
   to that app. (This bit the hub during GOL-1607; one clean single redeploy fixed
   it.)

4. **One-at-a-time discipline.** Deploy and verify one app fully before starting
   the next, or run them in parallel *only* if you track each app's single
   deployment id independently. Do not loop `create-deployment` over a list without
   a per-app poll gate.

5. **`doctl apps create-deployment` CREATES the deployment even when its output
   fails to parse.** Do **not** pipe it through a JSON parser that can mask
   success (`create-deployment -o json | python3 -c ...` emitted empty/non-JSON
   stdout in testing, so the wrapper raised "Expecting value" while the deployment
   was already created). If your capture step errors, **do NOT re-fire** — that is
   the double-deploy trap (#3). Instead run `doctl apps list-deployments "$APP"`
   and look for a deployment you just created before deciding anything. Capture the
   id with `--format ID --no-header`, never with a parser that can throw.
   *(Reproduced live on QA ggg 2026-08-18: an `-o json` create whose stdout didn't
   parse looked "failed", a second create fired 4s later, DO canceled the first,
   marked it failed, and auto-rolled-back — superseding both manual deploys. See
   Validation record.)*

---

## Production app IDs

| App | App Platform ID | Public URL |
|---|---|---|
| grove-hub-prod | `d5fa7795-da75-40e7-93fb-983e71558279` | https://gatheringatthegrove.com |
| grove-nursery-prod | `b9e0d2a6-6495-4dc7-a069-015b653c87e9` | https://atthegrovenursery.com |
| grove-goldberry-prod | `3da0b924-85f6-4531-859f-699e03c3cd74` | https://goldberrygrove.farm |
| grove-ggg-prod | `30c2a739-97d2-43bf-a6f8-dfff4a318bd8` | https://georgeggg.com |

QA counterparts (same per-tenant GHCR repos, same `latest`): hub `bcd3a29f…`,
nursery `aa671f09…`, ggg `173bfed0…`, goldberry `3b9c5625…`. **QA tracks the same
`latest`,** so QA's current build == the image a prod redeploy will pull.

---

## Preconditions

```bash
export PATH="/paperclip/.local/bin:$PATH"   # doctl, op, terraform live here
doctl account get                            # confirm DO auth (default context)
```

Under **Option A** (GOL-1304, pin-SHA policy) the release also bumps the pinned
image-tag var + runs `terraform apply` **before** the `create-deployment` step —
see *Option A addendum* at the bottom. Until GOL-1304 lands, apps track `latest`
and the deploy step alone rolls the current build.

---

## Procedure

### 1. Capture the BEFORE build fingerprint

The monorepo build fingerprint is the `webpack-<hash>.js` chunk name on the
rendered page. Same hash on prod as QA ⇒ prod is already current.

```bash
fp() { curl -s --max-time 20 "$1/" \
  | grep -oE 'static/chunks/webpack-[a-f0-9]+\.js' | head -1; }

# prod (via public URL or App Platform default_ingress) and QA, per tenant:
fp https://georgeggg.com                 # BEFORE (prod)
fp https://ggg.qa.gatheringatthegrove.com   # QA == current build target
```

Record BEFORE hashes for all four. For a full-parity check also compare the CSS
hash and the md5 of the sorted `/_next/static/chunks/*.js` set.

### 2. Confirm no in-progress deployment

```bash
APP=<app-id>
doctl apps get "$APP" -o json \
  | python3 -c 'import sys,json;d=json.load(sys.stdin)[0];ip=d.get("in_progress_deployment");print("in_progress:", (ip or {}).get("id"), (ip or {}).get("phase") if ip else None)'
```

Must print `in_progress: None None`. If a deployment is in progress, **wait** for
it — do not stack a second one (invariant #3).

### 3. Fire exactly ONE deployment

```bash
DEP=$(doctl apps create-deployment "$APP" --wait=false --format ID --no-header)
echo "deployment: $DEP"   # you will poll THIS id
```

> Capture the id with `--format ID --no-header` — **never** wrap the call in a
> JSON parser that can throw (invariant #5). If this step errors, the deployment
> may already exist: check `doctl apps list-deployments "$APP"` before doing
> anything, and do **not** re-run `create-deployment`.

### 4. Poll to a terminal phase — do NOT re-fire

```bash
DEP=<deployment-id-from-step-3>
# phases: PENDING_BUILD → BUILDING → PENDING_DEPLOY → DEPLOYING → ACTIVE (6 steps)
doctl apps get-deployment "$APP" "$DEP" --format Phase,Progress --no-header
```

Repeat until `Phase` is `ACTIVE` (success) or `ERROR`/`CANCELED`. Typical wall
time ~4–8 min. If it goes `ERROR`, read the build logs
(`doctl apps logs "$APP" --type build --deployment "$DEP"`) — do **not** blindly
re-deploy.

### 5. Prove the build rolled

```bash
fp https://georgeggg.com                 # AFTER (prod)
```

Pass criteria: prod AFTER hash **equals the QA hash** captured in step 1 (prod is
now on the current build). If `latest` had not advanced since prod's last deploy,
the hash is unchanged — that's still a valid pass (the deployment rolled the same
current image; the *mechanism* is proven by the ACTIVE deployment + matching-QA
hash). A prod hash that differs from QA after ACTIVE means QA and prod are pulling
different images — investigate before declaring success.

### 6. Smoke the public route

```bash
curl -s -o /dev/null -w '%{http_code}\n' --max-time 20 https://georgeggg.com   # expect 200
```

Repeat steps 2–6 for each remaining app.

### 7. Notify

Post a one-line status to the Grove ops Discord webhook (tag/build, apps rolled,
before→after fingerprints, operator).

---

## Rollback

App Platform retains prior deployments. To roll a storefront back to a known-good
build:

```bash
doctl apps list-deployments "$APP"                 # find the prior good deployment id
doctl apps create-deployment "$APP" --wait=false   # under `latest`, rolls current image
```

Under **Option A** (pinned tags) a rollback = set the image-tag var back to the
previous pinned SHA, `terraform apply`, then one `create-deployment`. **Do not**
double-fire (invariant #3) — one deploy, poll, verify. If a deploy fails and DO's
auto-rollback already restored the prior build, the app is already serving the old
(known-good) image; do not stack another deploy on top.

---

## Option A addendum (activates when GOL-1304 lands)

Option A pins `hub_image_tag` / `tenant_image_tag` to **immutable image SHAs** with
a validation block mirroring `custom_modules_ref`, and removes the (inert)
`deploy_on_push` blocks. The release then becomes:

1. Bump the pinned image-tag var(s) to the new SHA (the build published by
   grove-sites CI).
2. `terraform plan` (targeted) → review → `terraform apply` (updates the app spec).
3. **`doctl apps create-deployment` per app** (this is what actually rolls it —
   step 2 alone is not a deploy; invariant #2).
4. Poll to ACTIVE, prove fingerprint, smoke, notify (steps 4–7 above).

The `create-deployment` + single-fire + fingerprint-proof invariants are identical
under both `latest` and pinned-SHA modes.

---

## Validation record

- **2026-08-18 — QA rehearsal (GOL-1325):** exercised steps 1–6 against
  `grove-ggg-qa` (`173bfed0…`). Single `create-deployment` (`3b86186d…`), polled
  to ACTIVE, fingerprint verified. Confirms the mechanism, the single-fire
  discipline, and the poll gate on non-prod before the Aug-20 prod gate. _(See
  the GOL-1325 comment for the full before/after fingerprint capture.)_
