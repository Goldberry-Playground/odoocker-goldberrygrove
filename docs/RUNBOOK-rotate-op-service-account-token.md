# Runbook — rotate the 1Password service-account token (agent runtime)

Tracking issue: **GOL-2531**. Executor: **Josh** (1Password account admin — the
service account cannot rotate itself; `op` has no self-rotate verb and the
account is read-only on every vault, see
[`RUNBOOK-1password-agent-vault.md`](RUNBOOK-1password-agent-vault.md)).

Written so the rotation is a checklist, not an investigation. Everything an
agent could determine without admin rights is already filled in below.

## Which credential

| | |
| --- | --- |
| Env var | `OP_SERVICE_ACCOUNT_TOKEN` |
| Injected by | AgenticOS agent runtime, via `adapterConfig.env` |
| 1Password integration ID | **`WMSDNFU3FRCXVKGDQ7FTFKCCRI`** |
| `op whoami` → `User Type` | `SERVICE_ACCOUNT` |

The integration ID is the unambiguous handle — match on that in
**1Password → Developer → Service Accounts**, not on a remembered name.

## Why (GOL-2531)

A comment inside the double-quoted `ssh "<payload>"` string in
`scripts/prod-modules-promote.sh` contained Markdown backticks around the word
`printenv`. Double quotes make backticks command substitution, so the **local**
shell ran `printenv` at render time and spliced the agent runtime's whole
environment — this token included — into a payload string that was then
printed. Full analysis on GOL-2531; the recurrence guard is
`scripts/check-ssh-payload-escaping.py` (CI job "ssh payload render guard").

**The token value was never committed.** Verified 2026-09-23 across
`odoocker-goldberrygrove`, `grove-odoo-modules`, `grove-sites` and `AgenticOS`:
`git log --all -S` finds no token-shaped string on any ref, and every
`OP_SERVICE_ACCOUNT_TOKEN` hit in those repos is a *name* reference
(`${{ secrets.… }}`), never a value. Exposure is limited to the stored run
transcript and the model-provider request log for that session.

## Blast radius — read-only, but not low-impact

The token is read-only (it cannot create, edit, or delete: `(101) You do not
have permission`, and `op vault create` → `403`). What it *can* do, verified
live 2026-09-23 with the exposed token:

| Vault | Items readable |
| --- | --- |
| `Goldberry Grove - Admin` | 29 |
| `Grove Prod` | 6 |
| `Grove QA` | 7 |

So "read-only" bounds the *action*, not the *impact*: anyone holding this token
can read production secrets — and then use those downstream credentials at
their own, much higher, privilege. Treat this as a high-impact / low-probability
exposure (the transcript is not public), **not** a low-severity one. Rotate on
sight; there is no reason to leave it live.

## Rotate

1. **1Password → Developer → Service Accounts →** the account whose integration
   ID is `WMSDNFU3FRCXVKGDQ7FTFKCCRI`.
2. **Rotate / regenerate the token.** Keep the *same* service account and the
   *same* vault grants — this is a credential rotation, not a re-scoping. The
   permission matrix above is what the fleet expects; changing it here turns a
   5-minute rotation into a debugging session.
3. **Copy the new token once.** 1Password shows it exactly once.
4. **Re-inject into the agent runtime** as `OP_SERVICE_ACCOUNT_TOKEN` via
   `adapterConfig.env`. No repo change is needed — nothing hard-codes the value;
   every consumer resolves it from the environment.
5. **Revoke the old token** if the provider issues the new one alongside rather
   than in place of it. Rotation is only complete when the leaked value is dead.

## Check the other consumers before you close the tab

Rotation breaks any consumer still holding the old value. These GitHub Actions
secrets hold 1Password service-account tokens; **if any of them is backed by
integration `WMSDNFU3FRCXVKGDQ7FTFKCCRI`, it needs the new value too.** An agent
cannot read secret values to tell, so this is a human check:

| Repo | Secret | Used by |
| --- | --- | --- |
| `odoocker-goldberrygrove` | `OP_CI_SA_TOKEN` | `ci-failure-notify.yml`, `infracost.yml` |
| `odoocker-goldberrygrove` | `OP_CI_WRITEBACK_SA_TOKEN` | `otto-writeback-verify.yml` |
| `grove-sites` | `OP_CI_SA_TOKEN` | `docker.yml`, `preview-up/down/sweep.yml`, `cdn-asset-sync.yml`, `discord-digest.yml`, `discord-register-commands.yml` |
| `AgenticOS` | `OP_SERVICE_ACCOUNT_TOKEN` | `resize-droplet.yml`, `deploy-droplet.yml` |

Best practice, and the better answer if any of them *do* share the account:
give CI its own service account rather than sharing the agent runtime's. A
shared token means every future rotation is a fleet-wide coordination problem.

## Verify

Run from an agent shell after re-injection (no secret values are printed):

```sh
op whoami                    # expect User Type: SERVICE_ACCOUNT
op vault list                # expect the 3 vaults above, unchanged
op read "op://Goldberry Grove - Admin/perenual_api_key/credential" >/dev/null \
  && echo "resolve OK"       # any known-good ref; value intentionally discarded
```

All three must pass. `op whoami` succeeding on its own is not enough — it only
proves the token authenticates, not that the vault grants survived the rotation.

Then confirm the **old** token is dead. Josh, from a shell that does *not* hold
the new one:

```sh
OP_SERVICE_ACCOUNT_TOKEN='<old value>' op whoami   # expect an auth failure
```

If that still succeeds, the old token was not revoked and the rotation has not
actually closed the exposure.

## Rollback

There is none, by design — the old token is revoked. If the new token is
mis-scoped, the symptom is agents failing with `(101) You do not have
permission` or an empty `op vault list`; the fix is forward: re-grant the three
vaults in 1Password. Do not restore the leaked token.

## Don't reintroduce this

- CI job **"ssh payload render guard"** (`scripts/check-ssh-payload-escaping.py`)
  fails any PR that puts an unescaped backtick, `$(`, or ambient `${VAR}` inside
  an `ssh "<payload>"` string.
- **Never print a rendered ssh payload from a shell that holds secrets.**
  Inspect it with `bash -n` or run the guard. That single habit is what turned a
  typo into a credential rotation.
