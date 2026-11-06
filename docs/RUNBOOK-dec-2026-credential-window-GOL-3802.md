# RUNBOOK — December 2026 batched credential-rotation window (GOL-3802)

**Ruling (Josh, 2026-11-06, answer to `ask:GOL-2760:r2-probe-credentials`):** credential
rotations are batched into **one December 2026 window** instead of being minted one-off
per ticket. This is the single checklist for that window.
**Owner of the window:** Josh (every mint/revoke needs a console login an agent does not have).
**Prep + verification:** DevOps - Terra. **Tracker:** GOL-3802 — closing it `done` wakes
GOL-2760 → GOL-2755 (R2 probe → state-locking decision); GOL-2306, GOL-2956 and GOL-3503
are the other tickets waiting on it.

Everything in **Measured** below was read on 2026-11-06 from the agent plane with the
read-only 1Password service account; only status codes / fingerprints / prefixes were
read, never values. Re-measure any time, no secrets printed:

```sh
bash scripts/cred-window-verify.sh all        # or: do | cf | r2 | ssh | paperclip | stripe
```

## Ground rules for the whole window

- **Overwrite the existing 1Password field — do not create a sibling.** Every consumer
  below is wired to an `op://` reference by *item + field label*. Keeping the label means
  `.env.op` files, GitHub Actions `load-secrets` steps and Terraform vars pick the new
  value up with **zero code change**. New fields are only for credentials that do not
  exist yet (the R2 pair, `grove_prod_admin_ssh_public_key`, `do_token_grove_sites_ro`).
- **One value per field.** The 2026-10-05 outage (GOL-2956) happened because one DO token
  value had been pasted into three vault items; one console revoke killed all three. The
  same pattern is live again today (item 2) and in Cloudflare (item 3). Mint **distinct**
  tokens per consumer so a revocation has a known blast radius.
- **Revoke the old one last**, after the verify step for every consumer of that field is
  green. Revoking first turns a rotation into an outage.
- **Nothing here replaces a droplet.** The only Terraform change in the window is a tfvars
  value (item 1) whose plan is a no-op by construction (`ssh_keys` is in `ignore_changes`,
  pre-staged on `main`). If any `terraform plan` during the window shows `replace` or
  `destroy`, stop — that is not this runbook.
- Order of items is the order below: SSH key → DO tokens → Cloudflare + R2 → Paperclip →
  Stripe. Items 1–2 remove the agent-plane prod bypass first; 3 unblocks the critical path
  (state locking); 4–5 are independent and can slide.

---

## Item 1 — `grove-qa-admin` SSH key: stop it opening prod

**Why:** the private half lives on the shared agent droplet (`~/.ssh/grove_qa_admin`) *and*
in an agent-readable vault field, and the key is authorised on both prod droplets. With a
DO token that can edit the prod firewall (item 2) that is a standing bypass of the
prod `:22` allowlist (GOL-2306 / GOL-2282).

**Measured 2026-11-06**

| what | value |
|---|---|
| fingerprint (public, same everywhere) | `SHA256:gU+VD1s01lom0nAD33Je1tkv3LLOejmT1RTzcsFcLYI` (`grove-qa-admin@josh-Joshuas-MacBook-Pro-20260624`) |
| DO key record | id `57361722`, name `grove-qa-admin` |
| private copies | agent host `~/.ssh/grove_qa_admin` (+ `~/.ssh/config` host block for `grove-prod-odoo`); vault `Grove Infra/grove_qa_admin_ssh_private_key` (agent-readable) |
| surfaces that trust it | prod `grove-prod-odoo` (`production/odoo.tf:146`), prod `grove-prod-blogs` (`blogs.tf:121`), `grove-obs` (`observability/variables.tf` default), QA L3 (`qa-app-platform/main.tf:110`) |
| other DO keys, for reference | `grove-qa-deploy` (57362409, prod deploy path, private half **not** on the agent host), `grove-obs-deploy`, `agenticos-droplet-key`, 3× Josh Macs |

**Design (unchanged from `RUNBOOK-agent-plane-cred-hardening-GOL-2306.md`):** prod trusts
`grove-qa-deploy` + a new Josh-held `grove-prod-admin`; `grove-qa-admin` keeps QA/obs as its
home scope. Re-issuing the `grove-qa-admin` *material* is optional and **not** in this window:
once it no longer opens prod, its presence on the agent plane is QA-scoped by design, and
re-issuing it would force-replace `grove-obs` on the next apply (the shared droplet module
has no `ignore_changes` on `ssh_keys`).

**Mint (Josh, on the Mac)**

```sh
ssh-keygen -t ed25519 -C "grove-prod-admin@$(hostname -s)-$(date +%Y%m%d)" -f ~/.ssh/grove-prod-admin
# DO console → Settings → Security → SSH keys → Add → name EXACTLY: grove-prod-admin
ssh-keygen -lf ~/.ssh/grove-prod-admin.pub        # note the SHA256 fingerprint
```

**Vault:** `Goldberry Grove - Admin` / `Grove Infra` → new field `grove_prod_admin_ssh_public_key`
= the **public** key line only. The private half is Josh-held and must **never** land in a
vault the agent service account can read (that would recreate the exposure).

**Consumers to reconfigure**

| consumer | change | verify |
|---|---|---|
| `/root/.ssh/authorized_keys` on `grove-prod-odoo` (174.138.119.171) and `grove-prod-blogs` (159.203.131.236) | append the `grove-prod-admin` public line; **delete** the `grove-qa-admin` line (GOL-2306 runbook §A1 has the exact `grep -v` recipe) | from the Mac: `ssh -i ~/.ssh/grove-prod-admin root@<ip> true` → ok; `ssh -i ~/.ssh/grove-qa-admin root@<ip> true` → `Permission denied (publickey)` |
| `infra/terraform/environments/production/terraform.tfvars` (Josh-local, untracked) | add `prod_admin_ssh_key_name = "grove-prod-admin"` | `op run --env-file=.env.op -- terraform plan` → **`No changes`** (the data source re-resolves; `ssh_keys` is ignored). Any `replace` ⇒ stop |
| agent host (AgenticOS droplet) | `rm ~/.ssh/grove_qa_admin` and the `Host 174.138.119.171 138.197.44.60 grove-prod-odoo` block in `~/.ssh/config` | `bash scripts/cred-window-verify.sh ssh` → "absent (good)" |
| vault `Grove Infra/grove_qa_admin_ssh_private_key` | leave (QA scope) **or** delete if you also re-issue the key later; either way it must not open prod after the authorized_keys edit | the `Permission denied` check above is the proof |
| operator scripts/docs that say `-i ~/.ssh/grove-qa-admin` for **prod** boxes (`scripts/blogs-migrate.sh`, `scripts/caddy-prefer-prod-cert.sh` `SSH_KEY` default, `scripts/setup-all-ghosts.sh`, `docs/GHOST.md`, `docs/RUNBOOK-refresh-admin-ip-cidrs.md`) | pass `SSH_KEY=~/.ssh/grove-prod-admin` / `-i ~/.ssh/grove-prod-admin`; docs follow-up PR (Terra, not same-day) | n/a |
| GitHub Actions `PROD_SSH_PRIVATE_KEY` (CI fence, GOL-3414) | **untouched** — that is the `grove-prod-odoo` root deploy key, not `grove-qa-admin` | n/a |
| `grove-obs`, QA L3 | **untouched** this window (home scope) | n/a |

**Rollback:** re-append the `grove-qa-admin` line to `authorized_keys` from the Mac over the
`grove-prod-admin` session; revert the tfvars line. No droplet changes to undo.

---

## Item 2 — DigitalOcean API tokens: scoped, one per consumer

**Measured 2026-11-06** (`bash scripts/cred-window-verify.sh do`):

| vault field | status | scope seen (GET) | consumers |
|---|---|---|---|
| `Grove Infra/do_token` | **alive**, prefix `dop_v1_6c93a…` | account, droplets, firewalls, domains, databases, apps, spaces keys all **200** (full read; write scope unknown — the token list endpoint is 403 to it) | `production/.env.op`, `qa-app-platform/.env.op`, `state-backend/.env.op`, `cutover-archives/.env.op`, `preview` (tfvars example), `scripts/train-preflight.sh` (by field UUID), `.github/workflows/seed-preview-snapshot.yml` |
| `Grove Prod/odoocker/DIGITALOCEAN_TOKEN` | **alive — SAME VALUE as above** | same | GitHub Actions: `prod-plan-guard.yml` (required check), `terraform-drift.yml`, `qa-health.yml`, `firewall-membership.yml`, `obs-firewall-drift.yml`, `promote-storefronts.yml` (Josh-dispatched **apply**), `provision-*.yml` (fallback when `DO_FIREWALL_TOKEN` unset) |
| `AgenticOS Infra/do_token` | **alive — SAME VALUE as above** | same | AgenticOS repo workflows (`resize-droplet.yml`, `deploy-droplet.yml`); agent host (`doctl` config is the *old*, dead value — see below) |
| `Grove Infra/do_token_teardown` | **dead** (401) | — | `scripts/destroy-orphan-droplet.sh`, `make qa-l3-teardown[-all]` (`scripts/qa-l3-teardown.sh`) |
| `Grove Infra/do_token_scoped` | **dead** (401) | — | none on `main` (was the GOL-2956 stop-gap) |
| `Grove Infra/do_token_firewall_ci` | **alive**, prefix `dop_v1_5efe4…` | droplets 200, firewalls 200, everything else 403 — **correctly scoped** | GitHub Actions secret `DO_FIREWALL_TOKEN` (CI fence, GOL-3414) |
| grove-sites repo secret `DIGITALOCEAN_ACCESS_TOKEN` | **revoked** (GOL-3503) | — | grove-sites `DO App Spec — validate` workflow |
| agent host `~/.config/doctl/config.yaml` | file present, token **dead** since ≤2026-10-05 | — | nothing (the `doctl` binary is not installed there) |

So the three-copies-of-one-value pattern that caused GOL-2956 is back. The window fixes it
by minting **four distinct tokens** (plus keeping `do_token_firewall_ci` as is).

**Mint (Josh, DO console → API → Tokens → Generate → *Custom Scopes*)**. Name each token
after its vault field so the console list is self-documenting; set expiry to **1 year**
(`grove-*-2026-12`, note the date in this file's Measured table when you do).

| # | token name → vault field | custom scopes (read = `read`; write = `create`+`update`; add `delete` only where listed) | why this shape |
|---|---|---|---|
| 2a | `grove-infra-apply-2026-12` → `Grove Infra/do_token` (**overwrite**) | droplet r/w · firewall r/w · domain r/w + **delete** · database r/w · ssh_key read · reserved_ip r/w · volume r/w · spaces r/w · spaces_key r/w + delete · app r/w · project read · tag r/w · image read · region+size read · monitoring read | operator `terraform apply` for production / qa / state-backend / cutover-archives / preview. **No `droplet:delete`, no `app:delete`, no `database:delete`** — a routine apply must not be able to destroy; replaces/teardowns use 2b deliberately. `domain:delete` is needed for DNS record churn + `scripts/cleanup-acme-txts.sh`; `spaces_key:delete` for key rotation in `state-backend`. |
| 2b | `grove-infra-teardown-2026-12` → `Grove Infra/do_token_teardown` (**overwrite**) | everything in 2a **plus** droplet delete · app delete · volume delete · reserved_ip delete · firewall delete · database delete | `qa-l3-teardown.sh compute|all`, `destroy-orphan-droplet.sh`, board-gated prod rebuilds (`apply -replace`). This is the only token that can destroy; keep it off every CI secret. |
| 2c | `grove-ci-plan-2026-12` → `Grove Prod/odoocker/DIGITALOCEAN_TOKEN` (**overwrite**) | **read** on droplet · firewall · domain · database · ssh_key · reserved_ip · volume · spaces · spaces_key · project · tag · image · region · size · monitoring · account; **plus `app` read+create+update** | every CI workflow listed above is a `plan` or a read, except `promote-storefronts.yml`, which *applies* the four App Platform storefront apps — hence `app` write. If a promote 403s on some other resource, the error names it; widen 2c by that one scope rather than reaching for 2a. **No firewall write** — that stays on `do_token_firewall_ci`. |
| 2d | `agenticos-droplet-2026-12` → `AgenticOS Infra/do_token` (**overwrite**) | droplet read+update (resize/reboot of `agenticos-droplet` only), image read, size read | AgenticOS `resize-droplet.yml` / `deploy-droplet.yml`. No longer a copy of 2a. |
| 2e | `grove-sites-ro-2026-12` → **new** field `Grove Infra/do_token_grove_sites_ro` **and** grove-sites repo secret `DIGITALOCEAN_ACCESS_TOKEN` | app read (doctl only needs *a* valid token to `auth init`; `apps spec validate` is a local parse) | GOL-3503. |
| — | `do_token_firewall_ci` | **keep** (alive, correctly scoped) | only re-mint if you choose to revoke the whole old set; same scopes: droplet read + firewall read/update. |
| — | `do_token_scoped` | **delete the field** | no consumer. |

**Consumers to reconfigure** — none by code. Every `.env.op` and workflow already reads by
field; the agent host `doctl` file is the exception:

| consumer | change | verify (after each overwrite, before revoking anything) |
|---|---|---|
| all `.env.op` envs | nothing | `op run --env-file=infra/terraform/environments/production/.env.op -- terraform plan` → `No changes` (also run for `qa-app-platform` if QA is up, and `state-backend`) |
| `scripts/train-preflight.sh` | nothing | `bash scripts/train-preflight.sh` → DO token check passes |
| CI (2c) | nothing | open any trivial PR → `prod-plan-guard` + `terraform-drift` green; `qa-health` scheduled run green |
| CI fence | nothing | `provision-ceo-odoo-login.yml` `dry_run=true` → admit + revoke steps green |
| grove-sites (2e) | set repo secret | re-run grove-sites `DO App Spec — validate` (GOL-3503 names the run) → green |
| AgenticOS (2d) | nothing in repo; `op://…/AgenticOS Infra/do_token` resolves the new value | `resize-droplet.yml` dry-run / plan green |
| agent host | `rm -f ~/.config/doctl/config.yaml` | `bash scripts/cred-window-verify.sh ssh` → "absent (good)" |

**Revoke:** only after every row above is green, revoke the shared old value
(prefix `dop_v1_6c93a…`) in the DO console. Then `bash scripts/cred-window-verify.sh do`
must show **five different prefixes** across `do_token`, `do_token_teardown`,
`do_token_firewall_ci`, `odoocker/DIGITALOCEAN_TOKEN`, `AgenticOS Infra/do_token`, with
`do_token_teardown` the only one answering 200 on nothing-but-read probes *and* carrying
delete (the matrix cannot show delete; that is what the scope list in the console is for).

**Residual (not this window, flag for the board):** the agent service account reads
`Grove Infra` and `AgenticOS Infra` wholesale, so an agent can still `op read` the 2a apply
token. Scoping *tokens* fixes blast radius; scoping *what agents can read* needs a vault
split (`RUNBOOK-1password-agent-vault.md` pattern) — a separate ticket.

**Rollback:** the old value keeps working until you revoke it; paste it back into the field
if a consumer fails and you cannot widen the new token in the moment.

---

## Item 3 — Cloudflare API tokens + the R2 state-lock probe credentials

**Measured 2026-11-06** (`bash scripts/cred-window-verify.sh cf`):

| vault field | status | zones visible | consumers |
|---|---|---|---|
| `Grove Prod/Cloudflare API Token/credential` | **alive**, user-owned (`cfut_z…`), no expiry | 4 | `production/.env.op`, `cloudflare-policy/.env.op`, CI `prod-plan-guard.yml` / `terraform-drift.yml` / `promote-storefronts.yml` (`CLOUDFLARE_API_TOKEN`) |
| `Grove Infra/cf_zone_api_key` | **alive**, user-owned (`cfut_6…`), no expiry | **4** (QA only needs `gatheringatthegrove.com`) | `qa-app-platform/.env.op` (and the droplet's Caddy DNS-01 via `var.cloudflare_api_token`) |
| `Grove Prod/odoocker/CLOUDFLARE_API_TOKEN` | **alive — SAME VALUE as `cf_zone_api_key`** | 4 | **none on `main`** (GOL-1770 repointed CI off it) |
| `Grove Infra/account_cloudflare_api_token` | **dead** (`cfat_P…`, "Invalid API Token" on both verify endpoints, 0 zones; expired 2026-08-19) | 0 | none on `main` (`cloudflare-policy` repointed by #790) |
| `Grove Infra/cloudflare_analytics_read_token` | **alive**, account-owned (`cfat_w…`) | 4 | none in odoocker / grove-sites / AgenticOS code search — Josh to confirm (obs? a dashboard?) |
| R2 `r2_access_key_id` / `r2_secret_access_key` | **absent** | — | `docs/RUNBOOK-r2-state-lock-probe-GOL-2760.md` Step 1–3 (GOL-2760 → GOL-2755) |

**Mint**

| # | token → vault field | scope | why |
|---|---|---|---|
| 3a | `grove-prod-tf-2026-12` → `Grove Prod/Cloudflare API Token/credential` (**overwrite**) | **Zone** scope, all 4 zones (`goldberrygrove.farm`, `atthegrovenursery.com`, `woodworkingeorge.com`, `gatheringatthegrove.com`): Zone · Read; DNS · Edit; Zone Settings · Edit; SSL and Certificates · Edit; Dynamic Redirect · Edit; **Firewall Services · Edit; Cache Rules · Edit** | first five = `production/variables.tf` enumeration (verified live GOL-2317). The last two are the **GOL-2758 finding**: `cloudflare-policy` *apply* writes `http_request_firewall_custom` (geo blocks ×4) + `http_request_cache_settings` and plans clean without them, so they were never on the old token. Reconcile `variables.tf`'s description in the same follow-up PR. Set expiry 1 year. |
| 3b | `grove-qa-dns-2026-12` → `Grove Infra/cf_zone_api_key` (**overwrite**) | Zone scope, **`gatheringatthegrove.com` only**: Zone · Read; DNS · Edit | QA `qa.gatheringatthegrove.com` records + Caddy DNS-01. Today's token sees all four zones — down-scope. |
| 3c | **R2** — exactly `RUNBOOK-r2-state-lock-probe-GOL-2760.md` **Step 0**: bucket `grove-tf-state-probe`; R2 API token *Object Read & Write* scoped to that bucket only; S3 key pair → **new** fields `Grove Infra/r2_access_key_id` + `r2_secret_access_key`; post the account-id on GOL-2760 | the probe needs nothing more; an account-wide `Workers R2 Storage: Edit` token is explicitly *not* wanted. |
| — | `Grove Prod/odoocker/CLOUDFLARE_API_TOKEN` | **delete the field** (duplicate of 3b's old value, no consumer) | one value per field |
| — | `Grove Infra/account_cloudflare_api_token` | **delete the field** (dead since 2026-08-19) | and revoke it in the CF dashboard if it still lists |
| — | `Grove Infra/cloudflare_analytics_read_token` | keep **if** Josh names a consumer; otherwise revoke + delete | |

**Consumers to reconfigure** — none by code (same labels).

| consumer | verify |
|---|---|
| production TF | `op run --env-file=infra/terraform/environments/production/.env.op -- terraform plan` → `No changes` |
| cloudflare-policy TF | `op run --env-file=infra/terraform/environments/cloudflare-policy/.env.op -- terraform plan` → `No changes`; this is also the first time the token *could* apply that env — a deliberate `apply` here closes GOL-2758 #1 |
| CI | any PR → `prod-plan-guard` green (it loads the same field) |
| QA TF (if up) | `op run --env-file=infra/terraform/environments/qa-app-platform/.env.op -- terraform plan` → `No changes`; QA Caddy renews under the new token on its own schedule — check `docker logs caddy` for a DNS-01 failure after the next renewal |
| R2 | `bash scripts/cred-window-verify.sh r2` → both fields present; then GOL-2760 Step 1 — **exit 0 or 1 is a verdict, exit 3 means the creds/region are wrong, never a FAIL** |

**Revoke** the two old user tokens in CF → My Profile → API Tokens after the plans are green.
Then `cred-window-verify.sh cf` must show: prod token zones=4, QA token **zones=1**, and the
two deleted fields reported `FIELD MISSING`.

**Rollback:** old tokens keep working until revoked; paste back if a plan errors with
`9109`/`Authentication error`.

---

## Item 4 — Paperclip board key

**Measured:** `AgenticOS Infra/paperclip_board_key` exists and is agent-readable (same vault
grant as item 2's residual). `Grove Infra/paperclip_bridge_key` also exists. Neither is
consumed by anything in odoocker; the GitHub Actions secrets `PAPERCLIP_ISSUE_SYNC_*` and
`PAPERCLIP_QA_TRIAGE_*` are **webhook HMAC secrets**, not the board key, and are not in the
ruling.

**Rotate:** Paperclip → company settings → API keys → create a new board key → overwrite
`AgenticOS Infra/paperclip_board_key` → restart the AgenticOS consumers that hold it in env
(bridge / sync services on the agent droplet) → **then** revoke the old key in Paperclip.
If `paperclip_bridge_key` is the same value, rotate it with the same key; if nothing consumes
it, delete the field.

**Verify:** `PAPERCLIP_COMPANY_ID=<id> bash scripts/cred-window-verify.sh paperclip` → `200`
with the new value; from any shell, the **old** value against the same URL must answer `401`.
Agent heartbeats keep working (they use per-run JWTs, not the board key).

**Rollback:** keep the old key un-revoked until the `200` shows.

---

## Item 5 — Stripe env-var rename (GOL-3702 lineage) — **needs one confirmation**

**What is live today** (`production/.env.op`, `variables.tf`, `odoo.tf`, `cloud-init-odoo.yaml.tpl`,
`compose/docker-compose.odoo.yml`; mirrored in `qa-app-platform`):

| env var on the Odoo box | TF var | vault field | role |
|---|---|---|---|
| `stripe_secret_key_goldberry` | `stripe_secret_key_goldberry` | `Grove Prod/stripe/goldberry_stripe_key` | per-tenant, live since 2026-10-30 (#969) |
| `stripe_secret_key_nursery` | `stripe_secret_key_nursery` | `Grove Prod/stripe/atthegrovenursery_stripe_key` | per-tenant |
| `stripe_secret_key_ggg` | `stripe_secret_key_ggg` | `Grove Prod/stripe/ggg_stripe_key` | per-tenant |
| `stripe_test_secret_key` | `stripe_test_secret_key` | `Grove Prod/stripe/atthegrovenursery_stripe_key` (**same field as nursery**) | legacy global fallback; **misnamed** — it holds a LIVE `rk_live_` key, and `grove_headless._tenant_secret_key()` ignores it once any per-tenant key is set (fail-closed per tenant) |

**Terra's reading of "env-var rename":** retire the misnamed global `stripe_test_secret_key`
from prod (TF var, `.env.op` line, cloud-init line, compose line) now that all three tenants
have their own key, and rename the odd-one-out vault field `atthegrovenursery_stripe_key` →
`nursery_stripe_key` so the three fields match the slugs. **Josh confirms or corrects this
in the GOL-3802 confirmation before any PR** — it is a money-flow path (maintainer-protected,
Josh merges).

Once confirmed, the order is: (1) Terra PR renames the `.env.op` ref + drops the global var
(plan stays green: the var defaults to `""`, `user_data` is in `ignore_changes` ⇒ **no droplet
replace**); (2) Josh renames the vault field *after* that PR merges (the `op://` ref and the
field flip together); (3) converge the running box by hand exactly as on 2026-10-30:
edit `/etc/grove/.env`, `docker compose up -d odoo`; (4) verify.

**Verify:** `bash scripts/cred-window-verify.sh stripe` → three **distinct** `acct_` ids
(goldberry `acct_1RbD4B…`, nursery `acct_16cKwA…`, ggg `acct_1Ttsj9…`); one $0.50 live
checkout per storefront lands in its own Stripe account (GOL-3702 cutover recipe); prod
`terraform plan` → `No changes`.

**Rollback:** restore the `.env.op` line and the vault field name; the per-tenant keys are
untouched by this item, so checkout never depends on the rename landing.

---

## Closing the window

1. `bash scripts/cred-window-verify.sh all` from an agent shell → paste the output on GOL-3802.
2. Update the **Measured** tables above with the new prefixes/expiries (docs PR, Terra).
3. Mark GOL-3802 `done`. That wakes GOL-2760 (R2 probe, minutes), which cascades to GOL-2755;
   Terra then closes GOL-2306 (fingerprint proof), GOL-3503 (green run), and the CEO closes
   GOL-2956.

## Pre-staged on `main` (so window day needs no same-day PR)

- `production/variables.tf` `prod_admin_ssh_key_name` (default `grove-qa-admin`) → `blogs.tf`
  data source; **`ssh_keys` added to `ignore_changes`** on both prod droplets. Without that,
  the GOL-2306 §C key swap would have proposed a prod **replace** (DO provider: "Modifying
  this field will prompt you to destroy and recreate the Droplet") and tripped
  `prod-plan-guard`. The 2306 runbook carried the opposite claim; corrected there.
- `scripts/cred-window-verify.sh` — this runbook's verify steps, read-only.
- Every other consumer already reads by vault field label — nothing else to stage.
