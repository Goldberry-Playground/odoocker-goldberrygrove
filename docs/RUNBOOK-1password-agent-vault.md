# Runbook — agent-writable 1Password vault for QA secrets

Tracking issue: **GOL-2526**. Decision owner: **Josh** (1Password account admin).
Repeat victims of the chokepoint this removes: GOL-2518, GOL-2424, GOL-1643,
GOL-697, GOL-2318/2317.

This runbook is written **ahead of** the decision so that, if the answer is yes,
the work after Josh's five minutes is a single scripted verification and a
one-line `.env.op` edit — not another round trip.

## The chokepoint

The ops service account is **read-only on every vault it can see, and cannot
create a vault of its own.** There is no self-serve path: an agent that needs a
secret to *exist* stops until a human writes it.

Verified live 2026-09-23 against the real account
(integration `WMSDNFU3FRCXVKGDQ7FTFKCCRI`, `op whoami` → `SERVICE_ACCOUNT`):

| operation | `Grove QA` | `Goldberry Grove - Admin` | `Grove Prod` |
| --- | --- | --- | --- |
| `op vault list` / `op item get` | read | read | read |
| `op item create` | `(101) You do not have permission` | `(101) You do not have permission` | not attempted (prod) |
| `op item edit` | `(101) You do not have permission` | `Couldn't update the item.` | not attempted (prod) |
| `op vault create` | `(403) Forbidden: You aren't authorized to access this resource.` — account-level, not per vault | | |

The `op vault create` 403 is the important one: it closes the last avenue an
agent could have used to give itself a working home for QA secrets without
asking. **Do not re-probe these.** They cost nothing and prove nothing new; the
result is recorded here so the next agent doesn't spend a heartbeat on it.

`op service-account` in CLI 2.30.3 exposes only `create` and `ratelimit` — there
is **no CLI verb that edits an existing service account's vault access**. Josh
has to do this in the 1Password web UI. That is not a gap in our tooling.

## The options (GOL-2526)

- **A — dedicated agent-writable QA vault (recommended).** New vault, e.g.
  `Grove QA - Agent Managed`; ops SA gets read + write + create on *that vault
  only*. Prod and Admin unchanged. Reversible by deleting the vault.
- **B — write on `Grove QA`.** One permission change, no new vault, but an agent
  could overwrite a live human-managed secret (e.g. `stripe-nursery-qa`).
- **C — decline.** A legitimate posture: all vault writes stay human-only, and
  secret-provisioning latency is an accepted, known cost.

Everything below assumes **A**. Under B, skip step 1 and grant on `Grove QA`
instead; under C, delete this runbook.

## Josh's steps (~5 minutes, 1Password web UI)

UI labels drift between 1Password releases; the two destinations are what
matter, not the exact wording.

1. **Create the vault.** <https://my.1password.com> → *Vaults* → **New Vault** →
   name it exactly `Grove QA - Agent Managed`. Description: "QA-only secrets
   minted and rotated by agents. No prod, no money-flow keys. GOL-2526."
2. **Grant the service account.** *Developer* (a.k.a. *Integrations* /
   *Infrastructure Secrets Management*) → **Service Accounts** → the account
   with integration ID `WMSDNFU3FRCXVKGDQ7FTFKCCRI` → **Manage vault access** →
   **Add vault** → `Grove QA - Agent Managed` → enable **view / create / edit
   items**. Leave `Grove QA`, `Grove Prod` and `Goldberry Grove - Admin` exactly
   as they are (read only).
3. **Do not re-issue the token.** Editing vault access on an existing service
   account keeps the same token, so nothing in `.env.op`, CI, or the droplets
   needs rotating — the same edit-not-roll mechanic as the Origin CA token in
   GOL-2318. If 1Password ever forces a new token, say so on GOL-2526 instead of
   doing it silently: that turns a 5-minute change into a credential rollout
   across CI and every `op run` consumer.
4. Comment "granted" on GOL-2526. The agent verifies from there.

## Agent verification (run after the grant)

```bash
scripts/check-op-agent-vault-access.sh
```

It creates a throwaway item in the agent vault, reads it back, edits it, deletes
it, and prints one pass/fail line. It **refuses to run against `Grove QA`,
`Grove Prod` or `Goldberry Grove - Admin`** — a hard stop, not a warning, so the
probe can never touch a human-managed secret. It prints no secret values.

Exit codes: `0` write access confirmed · `1` vault reachable but not writable
(grant incomplete) · `2` vault not visible to the service account (step 1 or 2
missing) · `3` refused — guard tripped, `op` missing, or not signed in.

**What is verified today, before the grant:** the guard path (exit 3 on
`Grove QA`) and the not-visible path (exit 2 on the agent vault) were both run
live, and the probe's `op item create` invocation was validated with
`op item create --dry-run` (which renders locally and does not write). The
create/edit/delete *permission* outcomes cannot be exercised until the vault
exists — that is the first thing to run after step 2, and it is the acceptance
test for this runbook.

## Conventions once the vault exists

**Belongs in `Grove QA - Agent Managed`:**

- QA-only secrets an agent mints itself: HMAC webhook secrets, internal shared
  secrets, QA-scoped keys the agent generated.
- Anything whose loss is repaired by minting a new random value.

**Does NOT belong there — stays human-managed, read-only to agents:**

- Anything in `Grove Prod` or `Goldberry Grove - Admin`.
- Money-flow credentials (Stripe, Square, Odoo prod), *including QA/test-mode
  Stripe keys* — those live in `Grove QA` today and are human-issued, so they
  stay. The split is "did an agent mint it", not "is it QA".
- Third-party credentials a human had to obtain from a vendor portal
  (Cloudflare tokens, Davey/i-Tree, Anthropic). An agent may *store* one it was
  handed; the mint stays human.

**Naming:** `grove-<purpose>-qa`, one item per tenant where the secret is
per-tenant (`grove-publish-webhook-nursery-qa`), field `secret`. Use the item
**name** in `op://` refs, not the 26-char item ID — names survive an item being
recreated, IDs do not.

**Ref form** in `.env.op` (note the spaces in the vault name — keep the quotes):

```
TF_VAR_grove_publish_webhook_secret_nursery="op://Grove QA - Agent Managed/grove-publish-webhook-nursery-qa/secret"
```

**Audit:** every agent-written item carries a `note` field naming the issue that
created it. An item in that vault with no issue reference is an orphan and may
be deleted.

## Rollback

Delete the vault (items go with it), or remove the service account's access to
it in the same UI panel. Nothing outside that vault is affected — that is the
point of A over B. Any `.env.op` ref pointing at the deleted vault resolves to
empty, so re-comment those refs at the same time; on the QA app-platform
environment `make qa-l3-up` hard-gates on empty publish secrets
(`scripts/check-publish-webhook-secrets-wired.sh`) and will stop you before an
apply zeroes a live secret.

## Related

- `docs/RUNBOOK-publish-webhook-secrets.md` — the GOL-2518 instance of this
  chokepoint, including the zeroing landmine that guard now blocks.
- `docs/RUNBOOK-agent-odoo-keys.md` — the same human-in-the-loop pattern for
  Odoo API keys.
