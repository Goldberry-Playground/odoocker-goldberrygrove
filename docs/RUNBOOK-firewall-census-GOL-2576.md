# Runbook — the account-wide firewall census fired (GOL-2576)

**Alert:** `🧱 Firewall Membership` → *"A droplet is behind no firewall, or :22 is
open to the world"*
**Source:** nightly `.github/workflows/firewall-membership.yml`, census leg
**Script:** `infra/terraform/scripts/check-firewall-membership.py --census`

## What this leg asserts, and why it is separate

The other leg of that workflow compares live firewall membership against the
repo's `*.tf`. It is config-driven, so it can only assert things we declared —
an **un-codified droplet has no Terraform resource, therefore no expectation,
therefore no finding.** It is invisible by construction.

The census asks the DigitalOcean account instead of the repo:

1. every live droplet must be covered by **some** firewall, counting both
   explicit `droplet_ids` **and** tag-resolved membership;
2. no firewall may allow `:22` from `0.0.0.0/0` or `::/0` — **attached or not.**

Two of five droplets failed (1) on 2026-09-29 and one dormant firewall failed
(2). All three were found by a *hand-run* audit. Nothing scheduled would have
found any of them, which is the actual defect this leg fixes.

## Reproduce it yourself (read-only, safe anywhere, any time)

```bash
export DO_TOKEN=...                       # read scope is enough
./infra/terraform/scripts/check-firewall-membership.py --census
```

Pure `GET /v2/droplets` + `GET /v2/firewalls`. No Terraform, no state, no lock —
safe to run from the agent plane, mid-apply, or on a cron.

## Triage

### `UNCOVERED droplet <name> (id N, created ..., region ...): in NO cloud firewall`

That box is filtered by nothing at the DO edge. Whatever its host firewall does
or does not do, every listening port is reachable from the internet.

**Do not click a firewall on in the DO UI.** An un-codified droplet with a
hand-clicked firewall is a *second* snowflake, and the next `terraform apply`
neither knows nor preserves it. Codify it:

1. Add `infra/terraform/environments/production/<name>-fw.tf` declaring a
   `digitalocean_firewall` with `droplet_ids = [<id>]` and the narrowest
   inbound set that keeps the box working (`:22` from known `/32`s only).
   `agent-plane-fw.tf` (GOL-2569) and `legacy-ghost-fw.tf` (GOL-2566) are the
   worked examples.
2. `terraform -chdir=... plan -target=digitalocean_firewall.<label>` — confirm
   the plan is a **create only**, with no droplet replace anywhere in it
   (GOL-817: never bare-apply production).
3. Apply, then re-run the census. It should print `OK droplet ... behind ...`.
4. **Verify your own SSH still works** before you log out. Converging an
   allowlist is a lockout risk (GOL-1842).

Adopting the droplet into Terraform proper (`terraform import`) is better still,
but a firewall in front of it is the holding action and does not need to wait.

### `OPEN-SSH firewall <name> (id ..., N droplet(s), M tag(s)): ports '22' <- [...]`

A firewall that allows SSH from the whole internet.

- **`0 droplet(s), 0 tag(s)`** — dormant, so nothing is exposed *today*. It is
  still a finding: it is a loaded rule waiting for the first droplet someone
  attaches, or for a `tags` edit that auto-adopts one. **Delete it.**
- **Attached** — this is live world-open SSH. Narrow the rule to known `/32`s
  now, from code, and treat it as an incident.

### `ALLOWLIST ...`

An exemption entry is malformed or has passed its `expires` date. It has already
stopped suppressing (the underlying finding is printed too). Either fix the
exposure or re-review the tracking issue and re-date the entry.

## The allowlist

`infra/terraform/firewall-census-allowlist.json`. **An empty allowlist is the
goal state.** Entries require an `id`, a `GOL-NNNN` `issue` and a dated
`expires`; past that date the entry stops suppressing and becomes a finding
itself, so a "temporary" exposure cannot become permanent by nobody re-reading
the file. An accepted exposure is a reviewed commit, not silence.

## Expected state at merge (2026-09-29)

Verified read-only against the live account on 2026-09-29. Day one will be RED
with exactly three findings, each already owned:

| finding | id | clears when |
|---|---|---|
| `agenticos-droplet` in no firewall | 572389418 | GOL-2569 `agent-plane-fw.tf` is merged **and applied** |
| `ghostgoldberrygrove-nyc1` in no firewall | 468914087 | GOL-2566 `legacy-ghost-fw.tf` is merged **and applied** |
| `General` firewall `:22` ← `0.0.0.0/0` + `::/0` | `c6ca14ae-a47f-46c3-8327-db32cfc3ca75` | GOL-2570 — delete the firewall (0 droplets, 0 tags, nothing uses it) |

Note `General` is open on **both** stacks — the original audit recorded only
`0.0.0.0/0`; the census reads `::/0` as well.

This is a correct red, not noise: each line has one owner and one step, and the
board sees green the day the last one lands. The alternative — shipping the
allowlist pre-populated by the author of the check — would make the census pass
on day one while changing nothing about the exposure, which is the failure mode
this whole issue exists to remove.

## Managed database clusters (GOL-2582)

A third surface, added after the two above. Trusted sources live at
`/v2/databases/{id}/firewall` and are a `digitalocean_database_firewall`
resource — a different API object and a different Terraform resource from
everything under `/v2/firewalls`. No amount of droplet-firewall coverage
reaches them.

**The trap is the empty list.** On DO managed databases `trusted_sources: []`
is not "closed". The cluster's public host (`...:25060`) accepts connections
from **any** source that presents credentials, so the entire perimeter is the
`doadmin`/`odoo` password. A check that only asks "does the firewall object
exist?" reads an empty list as healthy.

| Finding | Means | First move |
|---|---|---|
| `OPEN-DB ... trusted sources are EMPTY` | Anyone with the password can connect | Find out **why** it is empty before re-applying (see below) |
| `OPEN-DB ... include the whole internet` | An `ip_addr` rule is `0.0.0.0` / `0.0.0.0/0` / `::` / `::/0` | Narrow it to the operator /32s |
| `UNKNOWN database ...` | The per-cluster read failed; posture unverified | Re-run; if it persists, check the token's database scope |

### Why it was empty, the time it mattered

`grove-qa-l3-pg` was found with `{"rules":[]}` on 2026-09-29. The allowlist had
been **codified all along** — `digitalocean_database_firewall.pg` in
`environments/qa-app-platform/main.tf`. It carried
`type = "droplet", value = digitalocean_droplet.odoo.id`, which makes it a
*dependent* of the droplet, and `qa-l3-teardown.sh compute` destroys with
`-target=digitalocean_droplet.odoo` — which destroys the target **and its
dependents**. The cluster survived on `prevent_destroy`; its allowlist did not.
So the exposure was continuous between trains, and re-applying the same resource
would only have reset the clock until the next teardown.

The fix (GOL-2582) is a `tag` rule: it takes a string, so the firewall depends on
the cluster and on `digitalocean_tag.pg_client` rather than on the droplet, and
the teardown leaves it standing. `prevent_destroy` on the firewall is the
tripwire against the droplet-id form coming back.

**Generalise this before you re-apply anything:** ask whether the resource is
absent because nobody wrote it, or because something routinely destroys it. Only
the first is fixed by applying.

### Note on `-target`

`terraform destroy -target=X` destroys X *and everything that depends on X*. The
dependents are never named in the teardown script, so a resource can be removed
every run by a command that does not mention it. To see the real blast radius
before trusting a targeted destroy:

```bash
terraform graph | grep -E ' -> "digitalocean_droplet\.odoo"'
```

Same lesson as the obs-droplet exemption (GOL-2472): absence from the target list
is not the same as survival — read it back.
