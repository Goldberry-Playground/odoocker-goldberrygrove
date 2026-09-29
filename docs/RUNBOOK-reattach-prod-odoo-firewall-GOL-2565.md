# Runbook — reattach `grove-prod-odoo-fw` to `grove-prod-odoo` (GOL-2565)

**Status:** awaiting Josh. Everything below is the operator half; the detection
and the post-apply proof are codified and already run from the agent plane.

**Severity:** production Odoo has had **no DO edge firewall since
2026-09-16**. `:22` answers the whole internet on both of its public IPs. This
is an *exposure*, not evidence of compromise — nobody has looked for
compromise, and that cannot be done from the agent plane.

---

## What drifted

`grove-prod-odoo-fw` exists, its **rules are correct**, and its membership is
empty:

| | |
|---|---|
| firewall | `grove-prod-odoo-fw` (`fc0ff910-3415-40ba-a849-ef05946111e4`) |
| `droplet_ids` | **`[]`** ← the drift |
| `tags` | `[]` |
| live `:22` sources | `173.84.140.152/32`, `74.47.41.38/32` (correct) |
| live `:80`/`:443` | `0.0.0.0/0`, `::/0` (correct) |
| droplet | `grove-prod-odoo` `601081550`, created 2026-09-16T14:38:41Z |
| public IPs | `138.197.44.60`, `174.138.119.171` |

`infra/terraform/environments/production/odoo.tf:394` codifies
`droplet_ids = [digitalocean_droplet.odoo.id]` correctly. The droplet was
**replaced** on 2026-09-16 (a `user_data` edit forces a REPLACE) and
`droplet_ids` was never re-converged afterwards. Same shape as the
`grove-obs-fw` drift in ADR-010.

This also explains GOL-2282 ("agent IP blocked on prod:22, not an incident").
That enforcement is **gone** — not because the allowlist changed, but because
the firewall holds no droplet.

## Why this is low-risk to fix

`droplet_ids` is **updatable in place**. Reconciling it is an in-place
firewall update, **not** a droplet replace — so no root-disk state is
destroyed and the filestore is untouched (contrast GOL-99 / GOL-93).

## Steps (Josh)

Per GOL-817 / GOL-391, **never bare-apply production.** `-target` throughout.

### 1. Plan, and read it carefully

```bash
terraform -chdir=infra/terraform/environments/production plan \
  -target=digitalocean_firewall.odoo
```

**Proceed only if the plan is exactly one in-place update**
(`~ droplet_ids [] -> [601081550]`, `Plan: 0 to add, 1 to change, 0 to
destroy`). If `digitalocean_droplet.odoo` shows **any** replace, **stop** and
put the plan on GOL-2565 — a replace here would destroy prod root-disk state
and is a different, board-gated decision.

### 2. Apply

```bash
terraform -chdir=infra/terraform/environments/production apply \
  -target=digitalocean_firewall.odoo
```

### 3. Confirm your own SSH still works — BEFORE you close anything

Converging an allowlist is itself a lockout risk (GOL-1842). From your
machine, in a session you still have open elsewhere:

```bash
curl -4 ifconfig.me                 # must be 173.84.140.152 or 74.47.41.38
ssh <you>@138.197.44.60 'echo ok'   # must succeed
```

If your ISP has rotated you off both codified CIDRs, **add the new address to
`admin_ip_cidrs` in `production/variables.tf` and re-apply before step 2** —
do not replace the existing entries (GOL-1842), append.

### 4. Post-apply proof (either of us can run this)

```bash
DO_TOKEN=<read-only> infra/terraform/scripts/check-firewall-membership.py production
# must print: OK grove-prod-odoo-fw: droplet_ids=[601081550] matches odoo.tf
# and exit 0
```

and the network side must flip — from the agent host `159.223.171.231`,
which is deliberately **not** in `admin_ip_cidrs`:

```
138.197.44.60:22   OPEN  ->  must become filtered/timeout
138.197.44.60:443  OPEN  ->  must STAY open
```

### 5. While you are on the box — check for use of the open window

Exposure window 2026-09-16 → the apply. Not part of the fix; do it anyway.

```bash
sudo grep -E 'Accepted (password|publickey)' /var/log/auth.log* \
  | grep -vE '173\.84\.140\.152|74\.47\.41\.38'
sudo lastlog; sudo last -F | head -40
```

Anything accepted from an address outside the allowlist → stop, do **not**
clean up, and escalate to the board as a suspected compromise.

## Recurrence guard (already codified, this PR)

- `infra/terraform/scripts/check-firewall-membership.py` — asserts every
  codified firewall *contains* the droplets its `*.tf` names, across all four
  envs. Read-only, lock-free, no Terraform.
- `.github/workflows/firewall-membership.yml` — runs it nightly and alerts the
  Discord ops webhook.

The existing GOL-2333 `observability/scripts/check-firewall.sh` compares
per-port source *unions*; it would have passed `grove-prod-odoo-fw` cleanly,
because the rules were never the problem. The two checks are complementary:
one asks "who may reach this port?", the other "is the box behind the firewall
at all?".
