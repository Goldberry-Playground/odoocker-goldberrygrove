# Runbook — the account-wide firewall census fired (GOL-2572)

> Filed under GOL-2572. GOL-2576 was cancelled as a duplicate of it, so an older
> link to `RUNBOOK-firewall-census-GOL-2576.md` is this file.

**Alert:** `🧱 Firewall Membership` → *"A droplet is unprotected, or a sensitive
port is open to the world"* (🚨, at least one ERROR) or *"A dormant firewall opens
a sensitive port to the world"* (⚠️, WARN only)
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
2. no firewall may allow a **sensitive port** from `0.0.0.0/0` or `::/0` —
   **attached or not.**

Two of five droplets failed (1) on 2026-09-29 and one dormant firewall failed
(2). All three were found by a *hand-run* audit. Nothing scheduled would have
found any of them, which is the actual defect this leg fixes.

## The port table, and why 80/443 are not in it

`SENSITIVE_PORTS` in the script is the authoritative list, with a one-line
"what this is" beside each entry: 22 SSH · 2375/2376 Docker daemon · 3000
Grafana/Next dev · 3306 MySQL · 3389 RDP · 5080 OpenObserve · 5432 PostgreSQL ·
5984 CouchDB · 6379 Redis/KeyDB · 8069/8072 Odoo direct · 9000 MinIO/Portainer ·
9090 Prometheus · 9200 Elasticsearch · 11211 memcached · 25060 DO managed DB ·
27017 MongoDB.

A bare `:22` check was the first cut and it is not enough — the same console
click that opens SSH opens Postgres, and GOL-2582 showed a data store sitting on
a default-open perimeter for an entire release train with nobody noticing.

**80 and 443 are deliberately absent from the table rather than allowlisted.**
They are world-open by design on `grove-prod-odoo-fw` and `grove-prod-blogs-fw`;
that is the product. Every allowlist entry must carry an owner and an `expires`
date, so a never-expiring entry for the front door would be a lie about what the
allowlist means — and a census that alarms on 443 on day one is a census
everybody mutes. A rule that reaches 80/443 *and* a table port (`ports: "0"`, a
wide range) still fires, on the table port, which is the correct reading.

## Severity: ERROR vs WARN

| | condition | meaning |
|---|---|---|
| **ERROR** | uncovered droplet; or world-open sensitive port on a firewall with `droplet_ids` **or** `tags` non-empty | something is reachable **now** |
| **WARN** | world-open sensitive port on a firewall with neither droplets nor tags | a loaded gun: inert today, live the moment anything attaches |

`tags` non-empty counts as ERROR even with `droplet_ids` empty: a tag firewall
**auto-adopts** any droplet wearing that tag, so its membership is whatever gets
tagged tomorrow, not what is listed today.

**Both exit non-zero.** The split routes urgency to the Discord title; it does
not gate the exit code. A nightly check that stays green while a world-open `:22`
firewall sits on the account is the same silence GOL-2565 was made of. If an
inert exposure is genuinely accepted, that belongs in the allowlist with an owner
and an expiry — not in the exit code.

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

### `WARN firewall <name> (id ..., 0 droplet(s), 0 tag(s)): world-open 22/SSH <- [...]`

A firewall that allows a sensitive port from the whole internet, with nothing
behind it. Dormant, so nothing is exposed *today* — still a finding: a loaded
rule waiting for the first droplet someone attaches, or for a `tags` edit that
auto-adopts one. **Delete the firewall.** If the board decides to keep it, add a
**port-scoped** allowlist entry (`"ports": [22]`) so accepting today's `:22` does
not silently pre-accept a `5432` added to it next week.

### `ERROR firewall <name> (id ..., N droplet(s), M tag(s)): world-open ... <- [...]`

Live. Narrow the rule to known `/32`s now, from code, and treat it as an
incident. `attaches BY TAG` on the line means membership is whatever wears the
tag — check `GET /v2/firewalls/<id>` for the tag and `GET /v2/droplets?tag_name=`
for who currently wears it before assuming the blast radius is zero.

### `ERROR ALLOWLIST ...`

An exemption entry is malformed or has passed its `expires` date. It has already
stopped suppressing (the underlying finding is printed too). Either fix the
exposure or re-review the tracking issue and re-date the entry.

## The allowlist

`infra/terraform/firewall-census-allowlist.json`. **An empty allowlist is the
goal state.** Entries require an `id`, a `GOL-NNNN` `issue` and a dated
`expires`; past that date the entry stops suppressing and becomes a finding
itself, so a "temporary" exposure cannot become permanent by nobody re-reading
the file. An accepted exposure is a reviewed commit, not silence.

Sections: `uncovered_droplets` and `open_port_firewalls` (the pre-widening name
`open_ssh_firewalls` is still read, so an entry written when this check only knew
about `:22` does not lapse the moment the table widened). A firewall entry may
carry `"ports": [22]` to scope itself; omit it and the entry covers every
sensitive port on that firewall and prints `UNSCOPED`.

## Expected state at merge (2026-09-29)

Verified read-only against the live account on 2026-09-29. Day one will be RED
with exactly three findings, each already owned:

| finding | id | clears when |
|---|---|---|
| `agenticos-droplet` in no firewall | 572389418 | GOL-2569 `agent-plane-fw.tf` is merged **and applied** |
| `ghostgoldberrygrove-nyc1` in no firewall | 468914087 | GOL-2566 `legacy-ghost-fw.tf` is merged **and applied** |
| `General` firewall `:22` ← `0.0.0.0/0` + `::/0` | `c6ca14ae-a47f-46c3-8327-db32cfc3ca75` | GOL-2570 — delete the firewall (0 droplets, 0 tags, nothing uses it) |

Severities: the two droplets are **ERROR**, `General` is **WARN** (`0 droplet(s),
0 tag(s)`). Re-verified read-only 2026-09-29 with the widened port table: the
only world-open ports anywhere on the account are `General`'s `:22`/`25565` and
the intentional `80`/`443` on the two prod firewalls. `grove-obs-fw`'s `:5080`,
`:3034` and `:8080` and every `:22` are `/32`-scoped, so widening the table added
**zero** new findings — it only removes the blind spot for the next rule.

Proven by simulation against that same live payload: deleting `General` and
dating the two droplet exemptions drops the run to `0 error(s), 0 warning(s)`
(exit 0), and *attaching* a droplet to `General` flips its line from WARN to
ERROR — which is precisely the case GOL-2572 was filed about, where the
membership guard in PR #745 would have reported the droplet safely covered.

Note `General` is open on **both** stacks — the original audit recorded only
`0.0.0.0/0`; the census reads `::/0` as well.

This is a correct red, not noise: each line has one owner and one step, and the
board sees green the day the last one lands. The alternative — shipping the
allowlist pre-populated by the author of the check — would make the census pass
on day one while changing nothing about the exposure, which is the failure mode
this whole issue exists to remove.
