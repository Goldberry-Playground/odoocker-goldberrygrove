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

| Leg | When | Command | What it does |
|-----|------|---------|--------------|
| **train-up** | Mon | `make train-up` | `= make qa-l3-up`. Idempotent `terraform apply` of the QA env. Droplets re-bootstrap from cloud-init; Odoo reconnects to the surviving Managed PG. **Safe to re-run.** |
| **train-teardown** | Thu | `make train-teardown` | `= make qa-l3-teardown` → `scripts/qa-l3-teardown.sh compute`. Destroys the 4 apps + the Odoo droplet + 2 volume attachments (the spend). Typed-confirm gated. **Data/DNS/certs survive**, and so does the exempt **grove-qa-l3-obs** droplet — see below. |

Preview before either (read-only, no spend, no lock-and-leave):

```bash
make qa-l3-plan          # dry-run for train-up  (terraform plan)
```

Teardown's dry-run is its **typed-confirm gate**: `scripts/qa-l3-teardown.sh
compute` prints the exact resource list and refuses to proceed until you type
`destroy-qa-l3-compute`. Any other input aborts before touching infra.

### The obs droplet is exempt from teardown (GOL-2333 / GOL-2472)

`compute` mode deliberately **does not** destroy `digitalocean_droplet.obs`
(**grove-qa-l3-obs**), its firewall, or the `oo.qa` / `keep.qa` DNS records.
Until the CEO ratifies ADR-010 (`docs/ADR/010-observability-droplet-home.md`,
which lands with PR #698), the exemption is enforced in code rather than in an
operator's memory:

- The `-target` list omits the obs droplet unless `QA_L3_TEARDOWN_OBS=1`.
- A **pre-flight tripwire** aborts with exit 3 if the obs droplet ever appears
  in the targets without that opt-in — so a bad merge or rebase costs a re-run,
  not a droplet.
- A **post-destroy check** re-reads `terraform state list` and exits non-zero
  unless the obs droplet, its firewall and both DNS records are still there.
  `-target` also destroys *dependents*, so absence from the target list is not
  by itself proof of survival; the check is the proof. A clean run prints
  `==> Exemption OK: obs droplet + firewall + oo/keep DNS records still in state.`

To include the obs droplet on purpose (after ratification, or to retire it):

```bash
QA_L3_TEARDOWN_OBS=1 make train-teardown
```

> **Not the same box.** This exemption is about **grove-qa-l3-obs**, the QA-only
> Phase-1.5 stack. The canonical observability plane — **grove-obs**, in
> `infra/terraform/environments/observability/` — has its own Terraform state
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
- **teardown obs exemption:** `python3 scripts/test_qa_l3_teardown_guard.py`
  runs the real script against stubbed `op`/`terraform` and asserts the obs
  droplet is not in the destroy targets. No network, no spend.
- **reminder:** `gh workflow run release-train-reminder.yml -f leg=up` (or
  `down`) posts a test embed to the ops Discord channel immediately.
