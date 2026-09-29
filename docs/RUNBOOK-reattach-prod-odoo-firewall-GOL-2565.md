# Runbook — reattach `grove-prod-odoo-fw` to `grove-prod-odoo` (GOL-2565)

**Status: RESOLVED 2026-09-29.** Josh applied a saved
`-target=digitalocean_firewall.odoo` plan (0 add / **1 change** / 0 destroy);
`droplet_ids` went `[]` -> `[601081550]` with no droplet change, and the two
hand-split console `:22` rules were merged back into the codified single rule
(same two admin IPs). Verified from both sides — see "Verification" below. The
steps are kept verbatim as the reference procedure for the next membership
drift; "Prevention" at the end is the part to read *before* the next prod
apply.

**Severity (historical):** production Odoo had **no DO edge firewall from
2026-09-16 to 2026-09-29** — `:22` answered the whole internet on both of its
public IPs. That was an *exposure*, not evidence of compromise; step 5 below is
the log check that would distinguish them.

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

## Verification (2026-09-29, post-apply)

Membership, from the DO API (read-only) — the codified guard from this PR run
against live prod:

```
$ DO_TOKEN=<read-only> infra/terraform/scripts/check-firewall-membership.py production
=== env production: 2 codified firewall(s)
  OK grove-prod-blogs-fw: droplet_ids=[593114105] matches blogs.tf
  OK grove-prod-odoo-fw:  droplet_ids=[601081550] matches odoo.tf
OK: every codified firewall contains the droplets its config names   (exit 0)
```

`grove-prod-odoo-fw` also reads `status: succeeded`, `pending_changes: []`, and
`:22` as **one** rule sourced `173.84.140.152/32` + `74.47.41.38/32`.

Enforcement, from the network side — probed from the agent host
`159.223.171.231`, which is deliberately **not** in `admin_ip_cidrs`. `rc=124`
is a 10s timeout, i.e. packets dropped at the DO edge; `rc=0` is a completed
handshake:

| target | before the apply | after the apply |
|---|---|---|
| `138.197.44.60:22` | OPEN | **rc=124, 10011ms — filtered** |
| `174.138.119.171:22` (2nd public IP) | OPEN | **rc=124, 10009ms — filtered** |
| `138.197.44.60:443` | OPEN | rc=0, 25ms — still open (correct) |
| `138.197.44.60:8069` | refused (host RST) | rc=124 — now dropped |
| `159.65.46.198:22` (grove-obs control, allowlists this host) | OPEN | rc=0, 27ms — OPEN |

The control probe is what makes the result meaningful: the same host at the same
moment still completes a handshake to an address that *does* allowlist it, so
the two prod timeouts are the firewall, not an egress block or a dead host. The
`:8069` flip from RST to drop is independent corroboration — an unfiltered host
refuses, a filtered one goes silent.

## Prevention — the rule that keeps this from recurring

**Root cause, stated precisely:** `production/odoo.tf` codifies
`droplet_ids = [digitalocean_droplet.odoo.id]` correctly. The 09-16 change was a
`-target`'d apply naming **only** the droplet. `digitalocean_firewall.odoo` is a
separate resource and was therefore not in the plan graph, so it kept pointing
at the destroyed droplet's id, which DO silently drops — leaving `[]`.

So, for every targeted prod apply from here on (now also in
`infra/terraform/environments/production/README.md` step 6 and the `deploy-test`
skill's droplet invariants):

1. **If the plan replaces a droplet, carry its firewall in the same `-target`
   set.** Never target a droplet alone.
   ```bash
   terraform apply -target=digitalocean_droplet.odoo  -target=digitalocean_firewall.odoo
   terraform apply -target=digitalocean_droplet.blogs -target=digitalocean_firewall.blogs
   ```
2. **Read membership back after the apply** — `check-firewall-membership.py <env>`
   must exit 0 and print the **new** droplet id. A clean apply is not proof: the
   09-16 apply was clean.
3. Treat every other by-id attachment on that droplet (reserved IP, volume
   attachment, monitor alert) the same way. Read the whole plan, not just the
   resource you set out to change.
4. The nightly watcher is the backstop, not the control — it bounds an undetected
   drift to ~24h. Steps 1–2 are what keep the window at zero.
