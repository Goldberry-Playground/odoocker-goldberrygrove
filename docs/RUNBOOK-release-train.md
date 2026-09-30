# Runbook — Grove Release Train (biweekly QA window)

**Owner:** DevOps (Terra) · **Epic:** GOL-2324 · **Automation issue:** GOL-2326
**Cadence:** biweekly. Train-up **Monday**, promote **Wednesday**, teardown **Thursday**.
**Train #1 anchor:** Mon **2026-09-21** (promote Wed **2026-09-23**, first
teardown Thu **2026-09-24**). The weekday is authoritative — 2026-09-25 is a
Friday.

The train exists to cut idle QA compute ~70%: the Level 3 QA env
(`infra/terraform/environments/qa-app-platform` — 4 App Platform apps + 2
droplets + 2 volume attachments) only runs during the ~3-day window, and is
torn down between trains. Durable state (Managed PG data, LE cert volume, DNS
zone, reserved IP) survives every teardown, so a rebuild is unattended.

---

## The one command per leg

Both legs are **local, human-run** (see the design decision below for why).
The Wednesday **promote** leg has no Makefile target and is written up
separately — see "The Wednesday promote leg" below.

| Leg | When | Command | What it does |
|-----|------|---------|--------------|
| **train-up** | Mon | `make train-up` | `= make qa-l3-up`. Idempotent `terraform apply` of the QA env. Droplets re-bootstrap from cloud-init; Odoo reconnects to the surviving Managed PG. **Safe to re-run.** Hard-gated on the publish-webhook secret guard — see below. |
| **train-teardown** | Thu | `make train-teardown` | `= make qa-l3-teardown` → `scripts/qa-l3-teardown.sh compute`. Destroys the 4 apps + the Odoo droplet + 2 volume attachments (the spend). Typed-confirm gated. **Data/DNS/certs survive.** There is no obs exemption any more — `grove-qa-l3-obs` was retired 2026-09-29 under ADR-010, see below. |

Preview before either (read-only, no spend, no lock-and-leave):

```bash
make qa-l3-plan          # dry-run for train-up  (terraform plan)
```

Teardown's dry-run is its **typed-confirm gate**: `scripts/qa-l3-teardown.sh
compute` prints the exact resource list and refuses to proceed until you type
`destroy-qa-l3-compute`. Any other input aborts before touching infra.

### If train-up aborts on the publish-webhook secret guard (GOL-2518)

`make train-up` now runs `scripts/check-publish-webhook-secrets-wired.sh` and
**refuses to apply** while any `TF_VAR_grove_publish_webhook_secret_<tenant>`
would resolve empty. That is deliberate: an apply from that state silently zeroes
the live per-tenant HMAC secret on both halves and kills the tenant's publish
path with no error anywhere (it happened to goldberry for ~8 weeks). The abort
names the tenants and the fix.

**Do the 5-minute fix, do not reach for the override.**
`docs/RUNBOOK-publish-webhook-secrets.md` → "Making it durable": create three
1Password items in vault `Grove QA`, then merge PR #731. Both steps need a
human — the ops service account is read-only on every vault, and `.env.op` is a
protected path.

`ALLOW_EMPTY_PUBLISH_SECRETS=1` exists for the case where you genuinely accept
zeroing them. While **GOL-2518** is open it is the wrong button: it re-breaks
nursery, which is the tenant the GOL-1896 sellout verification runs against.

**Teardown is the deadline, not the train.** `make train-teardown` destroys
`digitalocean_app.tenant` and `digitalocean_droplet.odoo`, which are the only
two places an un-vaulted secret lives. Any secret that is live-only and not in
1Password is **gone** after teardown, guard or no guard — the guard covers
applies, not destroys.

### No obs droplet in the QA env (retired 2026-09-29, ADR-010)

The QA-only **grove-qa-l3-obs** droplet and its firewall and `oo.qa` / `keep.qa`
records were retired when the CEO accepted ADR-010
(`docs/ADR/010-observability-droplet-home.md`, GOL-2333). The teardown
exemption (`QA_L3_TEARDOWN_OBS`, pre-flight tripwire, post-destroy state check)
went with it. There is nothing obs-shaped left in `qa-app-platform/` for
`train-up` to create or `train-teardown` to destroy.

> The canonical observability plane, **grove-obs** in
> `infra/terraform/environments/observability/`, has its own Terraform state
> and is never reachable by this script under any flag.

**Never run the DNS script as part of a teardown.** The qa zone and the
Cloudflare NS delegation survive every train; re-creating them burns the
Let's Encrypt issuance budget (ADR-005).

### Prerequisites (both legs)

- `op` CLI signed in with read access to the **`Goldberry Grove - Admin`** vault
  (Grove Infra item) **and** the **`Grove QA`** vault (Stripe/Shippo per-tenant
  items). Every secret is an `op://` ref in `qa-app-platform/.env.op`; `op run`
  resolves them into `TF_VAR_*` / `AWS_*` for the wrapped terraform. Values
  never touch shell history.
- `terraform ~> 1.10`.

---

## The Wednesday promote leg

`train-up` and `train-teardown` each have a Makefile target; **promote does
not**, and that is deliberate — it is two independent legs against production
with a human approval between them. Train #1 ran this leg ad hoc, which is how
both footguns in "Retired caveats" below were found live on a revenue box. It
is written down here so Train #2 onward runs it the same way twice.

**Who runs it: Josh, from his own shell.** Not an agent, and not CI. The prod
droplet's DO firewall does not admit the agent plane on port 22 — a TCP connect
to the reserved IP hangs to timeout (GOL-2282; re-confirmed 2026-09-30). Leg A
also stops at the `production` GitHub Environment gate, which only a configured
reviewer can release.

### Pin the bundle first

Pin **explicit 40-char SHAs** for both repos before anything runs — one
`grove-odoo-modules` SHA and one `grove-sites` SHA — and put them in the train
issue. Never promote "main at promote time": main moves under you, and an
out-of-train hotfix during the window will ride along unreviewed (it did during
the 2026-09-29/30 GOL-2677 hotfix). Pinning is also what releases any PRs held
back for the next train.

### Order of operations

| # | Step | Command |
|---|------|---------|
| 1 | **Gate on QA, at the pinned modules SHA.** `EXPECT_REF` makes the script refuse a QA box that is not actually on the bundle SHA. | `EXPECT_REF=<modules-sha> scripts/qa-module-upgrade.sh grove_headless` |
| 2 | **e2e LAST.** Dispatch `e2e-nursery.yml` only after step 1 and after the final merge. Read the skip list — a spec that skipped is not a spec that passed. | — |
| 3 | **Leg B pre-flight** (writes nothing; reports the recorded version and whether the tax migration is due). | `TARGET_REF=<modules-sha> scripts/prod-modules-promote.sh` |
| 4 | **Leg B promote.** Rewrites `CUSTOM_MODULES_REF` in the droplet's `/etc/grove/.env`, waits for git-sync, runs the migrations, then **proves** the WV tax bound for every company. | `TARGET_REF=<modules-sha> CONFIRM=PROMOTE scripts/prod-modules-promote.sh` |
| 5 | **Reconcile the committed pin** onto what is now live, or the next droplet rebuild rolls prod back. Drift-only; never touches prod. Merge the PR it opens. | `gh workflow run reconcile-modules-pin.yml -f modules_sha=<modules-sha>` |
| 6 | **Leg A storefronts.** Then approve the `production` Environment gate, and merge the reconcile PR it opens. | `gh workflow run promote-storefronts.yml -f target_sha=<sites-sha> -f confirm=PROMOTE` |

Steps 3–5 are Leg B (modules) and step 6 is Leg A (storefronts); the script
header explains why they cannot be one workflow. **Order matters when a
storefront change depends on a backend change** — promote modules first, so the
frontend never goes live against an API that does not have its field yet.

### Retired caveats — do NOT carry these forward

Both workarounds that Train #1 needed were fixed on 2026-09-30 and are now
wrong advice:

- **`PROD_HOST` needs no override.** It defaults to prod's reserved IP, and the
  script now refuses a Cloudflare-proxied hostname up front instead of hanging
  on a connect that can never complete (#787).
- **The first attempt no longer exits 6.** Bulk-sourcing `.env` used to export
  the *old* `CUSTOM_MODULES_REF` into the payload shell, where compose
  interpolation prefers it over `--env-file`, so git-sync was recreated on the
  stale ref and the 300s wait timed out (#783, GOL-2657).

**So run the promote once. A failure now is a real failure** — stop and
diagnose it. Do not re-run on the assumption that the second attempt sticks;
that assumption is exactly what hid GOL-2657 for a full train.

### Rollback

Leg A rolls back by re-running `promote-storefronts.yml` at the previous pin.

Leg B does not. The script backs `/etc/grove/.env` up before it writes and
prints the exact restore commands on both the failure and success paths, but
**Odoo does not down-migrate**: once the upgrade pass has run, reverting the pin
returns the *code* and not the *schema*. Treat a Leg B rollback as an incident,
not a routine undo — which is why step 3's pre-flight tells you whether a
migration is due before you commit to step 4.

### Verification

- **Step 4 is self-verifying, and its success condition is not "exit 0".**
  `setup_wv_sales_tax` swallows per-company failures at WARNING, so a partial
  bind exits 0 and looks fine while some companies mis-charge tax. The script
  asserts the `WV 6% state sales tax bound for N of N companies` line *and*
  re-reads the tax from the live DB.
- **Read the live pin, not the PR badge.** `/etc/grove/.env`'s
  `CUSTOM_MODULES_REF` is the truth for Leg B; the storefront build fingerprint
  is the truth for Leg A. A merged reconcile PR only means committed HCL now
  agrees with what was already live.
- `scripts/test_prod_modules_promote.py` (27 tests, hermetic — no droplet) is
  the regression suite for this leg, including the two retired caveats above.

---

## Cadence automation — scheduled reminder (not scheduled apply)

`.github/workflows/release-train-reminder.yml` posts a **Discord** reminder on
the train cadence:

- **Mon 13:00 UTC** → "run `make train-up`"
- **Thu 13:00 UTC** → "run `make train-teardown`"

Biweekly is enforced by a parity guard anchored to Train #1 (`2026-09-21`):
`(days since anchor) / 7` — even week index ⇒ train week; off-train weeks fire
nothing. The guard is stateless (no stored counter) so it self-corrects across
skipped runs. `workflow_dispatch` with `leg=up|down` forces a test reminder.

The workflow reads **only** `DISCORD_OPS_WEBHOOK_URL` (Grove Prod vault, via the
existing read-only `grove-ci-prod-ro` SA — same token qa-health.yml and
terraform-drift.yml already use). **No new secret, no CI ability to spend or
destroy.**

---

## DESIGN DECISION — reminder + local one-command, NOT a CI apply/destroy

**Question (GOL-2326):** should train-up/teardown run *in CI* (with a manual
approval gate) or stay *local one-command* with only a scheduled reminder?

**Decision: local one-command + scheduled reminder.** Rationale:

1. **Creds don't live in CI, and putting them there is the wrong trade.**
   `qa-l3-up` resolves `op://` refs across **two** vaults — `Goldberry Grove -
   Admin` (the master DO token, Cloudflare token, state-backend keys) and
   `Grove QA` (Stripe/Shippo). The only 1Password token CI holds is the
   **read-only** `grove-ci-prod-ro` SA, scoped to **Grove Prod** only. A CI
   apply would require minting a CI SA that reads the Admin vault — i.e. giving
   the CI plane standing read of the token that can create/destroy *all* Grove
   spend. That is a materially larger blast radius than the convenience buys.

2. **Teardown needs a delete-scoped token CI deliberately lacks.** Per
   `scripts/qa-l3-teardown.sh`'s header, "destroys are not CI material: the
   drift workflow's token deliberately can't delete." Codifying teardown as CI
   would mean a *delete-capable* DO token living in CI secrets — the exact
   least-privilege line the current design holds.

3. **Josh approval on spend is intrinsic, not bolted on.** The human who holds
   Admin-vault access is the human who runs `make train-up`. There is no
   unattended path to spend, which is the property the issue asked for. A CI
   `environment:` approval gate would re-create that property at the cost of
   (1) and (2).

4. **Reliability — the real gap — is solved by the reminder.** The failure mode
   the epic worries about is "nobody ran teardown, so QA burned compute all
   fortnight." A cadence-accurate Discord nudge closes that gap without moving
   any credential.

**Rejected alternative — CI `workflow_dispatch` + `environment:` approval:**
technically feasible (the repo already gates `release.yml` behind
`environment: production`), but it forces the Admin-vault-SA and delete-token
expansions above. Revisit only if the reminder proves insufficient *and* a
scoped, delete-only, QA-only DO token + an Admin-read SA scoped to *just* the
QA env's items can be minted — i.e. when least privilege can be preserved. Until
then, local + reminder is the least-privilege answer.

**Confirm with:** CEO / Josh (spend + credential-placement call). Tracked on
GOL-2326.

---

## Verification

- **train-up dry-run:** `make qa-l3-plan` (read-only `terraform plan`). The same
  plan runs green in CI on every `terraform-drift.yml` execution against this
  exact state, so the path is continuously proven.
- **train-teardown dry-run:** run `scripts/qa-l3-teardown.sh compute` and enter
  anything other than `destroy-qa-l3-compute` at the prompt — it aborts without
  calling terraform. That typed-confirm IS the safe dry-run.
- **reminder:** `gh workflow run release-train-reminder.yml -f leg=up` (or
  `down`) posts a test embed to the ops Discord channel immediately.
