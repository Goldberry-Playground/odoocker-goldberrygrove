# Runbook — CI SSH through the prod Odoo fence (GOL-3414)

**Status:** codified; **inert until Josh's one-time setup below** (secrets +
one prod apply). Until then every `environment=prod` dispatch of the three
provisioning workflows fails in `Add droplet to known hosts` with a message
that names this runbook — which is strictly better than the silent
`2>/dev/null` timeout it would have hit before.

Sibling of AgenticOS `docs/runbooks/ci-ssh-agent-plane-fence.md` (GOL-3412).
Same mechanism, same action, different droplet and firewall.

## What this covers

| workflow | trigger | SSHes as | fenced leg |
|---|---|---|---|
| `provision-project-agents.yml` | `workflow_dispatch` (environment qa/prod) | `root@$PROD_HOST` / `root@$QA_HOST` | prod only |
| `provision-logistics-otto.yml` | `workflow_dispatch` (environment qa/prod) | `root@$PROD_HOST` / `root@$QA_HOST` | prod only |
| `provision-ceo-odoo-login.yml` | `workflow_dispatch` (prod, GitHub environment `production`) | `root@$PROD_HOST` | always |

`grove-prod-odoo-fw` (`fc0ff910-…`, odoo.tf, GOL-2565, re-attached 2026-09-29)
admits tcp/22 on `grove-prod-odoo` **only from `admin_ip_cidrs`** (two
operator /32s). GitHub-hosted runners egress from ~7,000 rotating Azure
CIDRs. None are admitted, so a prod dispatch's `ssh-keyscan` gets nothing —
exactly what took down ten AgenticOS workflows on 2026-10-15 (GOL-3412).

QA droplets are ephemeral per-PR / per-train boxes with no DO fence, so the
`environment=qa` leg keeps the plain keyscan path. Nothing in this change runs
on QA except the (now retrying, stderr-preserving) keyscan.

## How it works

Before the keyscan, a prod job runs AgenticOS's
`.github/actions/agent-plane-fence` (pinned by SHA — `de4cc450…`, the #899
merge) with `mode: admit`, `droplet-name: grove-prod-odoo`,
`firewall-name: grove-prod-odoo-ci-fw`:

1. resolves the DO token (see **Token** below), masks it, keeps it in the step
   env only;
2. finds — or on first use **creates** — `grove-prod-odoo-ci-fw`, a second DO
   cloud firewall attached to `grove-prod-odoo` with **no inbound rules** and
   the same wide-open outbound as the fence;
3. adds `<runner public ip>/32 → tcp/22` to **that** firewall, waits for DO to
   report it applied, probes `$PROD_HOST:22`, and exports
   `AGENT_PLANE_CI_FW_ID` / `AGENT_PLANE_CI_RUNNER_CIDR` to the job env.

The job's last step before `Summary` runs the same action with `mode: revoke`
under `if: always()`: it deletes exactly that rule and verifies it is gone.
With nothing recorded (job failed before admit, QA leg) revoke is a no-op.

DO **unions** every firewall attached to a droplet, so the runner is admitted
while `grove-prod-odoo-fw` is untouched: no Terraform drift on the managed
fence, and anything a killed runner leaves behind is confined to a firewall
whose name says who owns it. The admit step warns once the CI firewall carries
5+ rules (stray /32s from killed runners) — prune them in the DO console or
with `doctl compute firewall remove-rules`.

The firewall is codified in
`infra/terraform/environments/production/prod-odoo-ci-fw.tf` as
`digitalocean_firewall.odoo_ci` (count-gated on
`prod_odoo_ci_firewall_enabled`, default `true`) with
`lifecycle { ignore_changes = [inbound_rule] }`, so CI's transient /32s never
plan as drift and an apply never strips one out from under a running job. The
nightly `firewall-membership.yml` check asserts the droplet is behind it once
it exists (it SKIPs a count-gated firewall that is not live yet).

### Token

The action takes the DO token from, in order:

1. `secrets.DO_FIREWALL_TOKEN` — **preferred.** A DO token with custom scopes
   `droplet:read`, `firewall:read`, `firewall:update`, `firewall:create`
   (create only so the action can rebuild the CI firewall if it is ever
   deleted). This is the same token Josh minted for AgenticOS on 2026-10-21
   (`Grove Infra` → `do_token_firewall_ci`); set the same value here.
2. Fallback when (1) is empty: `op read "op://Grove Prod/odoocker/DIGITALOCEAN_TOKEN"`
   via `secrets.OP_CI_SA_TOKEN` — the **same service account and the same
   op:// reference** `prod-plan-guard.yml`, `terraform-drift.yml`,
   `firewall-membership.yml` and four other prod workflows in this repo
   already read. It grants CI **no reach it does not already have**, which is
   why the fallback is wired instead of blocking the fix on a new secret.
   But that token is the account-wide Terraform token, so it is still the
   wider of the two: set `DO_FIREWALL_TOKEN` and the fallback is never
   consulted.

Either way the token is masked and lives only in the step environment; it is
never written to disk.

## One-time setup (Josh)

These three workflows have **never been dispatched** (0 runs as of
2026-10-21) and the secrets they reference do not exist at repo, environment
(`production` has only `OP_CI_SA_TOKEN`) or org level. The fence fix is
necessary but not sufficient — the secrets are the other half.

1. **Seed the SSH secrets** (repo-level; the workflows read `secrets.*`
   directly). Values from 1Password → `Grove Prod`:
   ```bash
   R=Goldberry-Playground/odoocker-goldberrygrove
   gh secret set PROD_HOST            --repo $R   # grove-prod-odoo public IP (138.197.44.60) or its hostname
   gh secret set PROD_SSH_PRIVATE_KEY --repo $R < ~/.ssh/grove-prod-odoo    # root deploy key
   gh secret set QA_HOST              --repo $R   # current QA droplet (only if you will run the qa leg)
   gh secret set QA_SSH_PRIVATE_KEY   --repo $R < ...
   gh secret set OP_CI_WRITEBACK_SA_TOKEN --repo $R   # live runs only; docs/RUNBOOK-otto-key-writeback.md
   ```
   `gh secret set` prompts for the value when no stdin is given; nothing lands
   in shell history.
2. **(Preferred) set the scoped firewall token** — same value as AgenticOS:
   ```bash
   gh secret set DO_FIREWALL_TOKEN --repo Goldberry-Playground/odoocker-goldberrygrove
   ```
   Skip this and the fallback in **Token** applies.
3. **Apply the production Terraform** so the firewall exists and is owned
   before the first dispatch. Expected plan: exactly
   `1 to add, 0 to change, 0 to destroy` (`digitalocean_firewall.odoo_ci[0]`).
   ```bash
   cd infra/terraform/environments/production
   op run --env-file=.env.op -- terraform plan -target=digitalocean_firewall.odoo_ci -out=ci-fw.plan
   op run --env-file=.env.op -- terraform apply ci-fw.plan
   ```
   **Ordering hazard:** if a prod dispatch ran *before* this apply, the action
   already created `grove-prod-odoo-ci-fw` out of band. Check with
   `doctl compute firewall list --format ID,Name,DropletIDs`. If it is there,
   do **not** let apply create a second one with the same name — add an
   `import { to = digitalocean_firewall.odoo_ci[0]  id = "<uuid>" }` block to
   `prod-odoo-ci-fw.tf` (the pattern `agent-plane-fw.tf` uses) and re-plan;
   expected `1 to import, 0 to add`.
   Until this apply lands, `terraform-drift.yml` will report the pending add
   nightly (exit 2) — that is this change waiting, not account drift.

## Verify

Dispatch the read-only shape of the broadest caller:

```bash
gh workflow run provision-project-agents.yml \
  --repo Goldberry-Playground/odoocker-goldberrygrove \
  -f environment=prod -f dry_run=true
```

`dry_run=true` only prints the roster scope preview inside `odoo shell` — it
creates no users and mints no keys. In the run:

- `Admit this runner through the prod Odoo fence` prints
  `Admitting <ip>/32 on tcp/22 via grove-prod-odoo-ci-fw (<id>)` and
  `tcp/22 on <PROD_HOST> reachable from this runner`.
- `Add droplet to known hosts` prints `Host key pinned on attempt 1/5.`
- `Revoke this runner from the prod Odoo fence` prints `Revoked.`

Then confirm the fence itself is unchanged and the CI firewall is empty again:

```bash
doctl compute firewall get fc0ff910-3415-40ba-a849-ef05946111e4 --format InboundRules   # still only admin /32s on :22 + 80/443 world
doctl compute firewall list --format ID,Name,InboundRules | grep grove-prod-odoo-ci-fw  # InboundRules empty
```

That is GOL-3414's done bar: a prod dispatch reaches the keyscan step green
with the prod fence unchanged.

## Rollback

- **Stop admitting runners:** revert the three workflow files (the admit /
  revoke steps are self-contained; the retrying keyscan is harmless to keep).
- **Remove the firewall:** set `prod_odoo_ci_firewall_enabled = false` and
  apply (`1 to destroy`, the CI firewall only — `grove-prod-odoo-fw` and the
  droplet are untouched). Detaching a DO cloud firewall is immediate and does
  not touch the droplet.
- **Stray /32 left by a killed runner:** `doctl compute firewall remove-rules
  <ci-fw-id> --inbound-rules "protocol:tcp,ports:22,address:<ip>/32"` — or
  just let the next successful job's canary warning tell you.

## Residual risk (accepted, same as GOL-3412)

DO custom-scoped tokens are **account-wide**: `firewall:update` can alter any
firewall on the account, including `grove-prod-odoo-fw` and
`grove-agent-plane-fw`. The action only ever touches the firewall whose name it
is given, but the credential's blast radius is the account's firewalls, not one
of them. It cannot touch droplets, volumes, Spaces, DNS or other tokens.

Known nit carried over from the AgenticOS action (non-blocking): the admit
step writes `AGENT_PLANE_CI_*` to `$GITHUB_ENV` only *after* the rule POST and
the settle wait, so a cancel between those two moments leaves one /32 behind
until the 5-rule canary or a manual prune.

## Pre-existing oddity (not changed here)

`provision-ceo-odoo-login.yml` SSHes to **prod** but `cd`s to `/etc/grove`
and runs a bare `docker compose exec`, while the other two prod legs use
`/opt/grove` with the `grove` + `production` override files. One of those is
wrong for the current prod layout; it is orthogonal to the fence and left
as-is — verify on the first live dispatch.
