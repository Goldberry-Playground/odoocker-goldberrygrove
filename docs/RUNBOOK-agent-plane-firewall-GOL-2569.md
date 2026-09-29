# RUNBOOK — fence the agent plane (`agenticos-droplet`) behind a cloud firewall

**Issue:** GOL-2569 · **Operator:** Josh · **Est. time:** 10 min
**Codified in:** `infra/terraform/environments/production/agent-plane-fw.tf`
(read its header comment first — it carries the full rationale and the measured
exposure this runbook acts on).

> **Read this line before anything else.** This is the host every Grove agent
> runs on. The only way this change hurts is if it takes away *your own* SSH.
> Step 1 exists to stop that, step 5 exists to catch it, and the DO Recovery
> Console is out-of-band and works even if the firewall locks the front door.

---

## Why

`agenticos-droplet` (`572389418`, `159.223.171.231`, created 2026-05-21) is in
**no** DO cloud firewall, and it holds an **account-wide, non-read-scoped
DigitalOcean API token** (`~/.config/doctl/config.yaml`), the `grove_qa_admin`
Odoo credentials, the GitHub broker key and the Paperclip API key — plus a
codified SSH/OTLP vantage into `grove-obs` (`grove-obs-fw` admits
`159.223.171.231/32` on `:22` and `:5080`). An unfiltered box holding that token
can delete the very firewalls GOL-2565/GOL-2566 are about.

**Measured exposure (2026-09-29, read-only):** `tcp/22` (sshd) and `tcp/8384`
(Syncthing v1.30.0 GUI/REST, bound `0.0.0.0`, auth on — unauthenticated
`/rest/system/status` → 403). Nothing else in 1–10000 or on the service list.
No inbound `:80`/`:443`/`:3100` at all, so **the Paperclip dashboard is served by
an outbound tunnel and an inbound fence cannot break it.**

## What the change does

Adds `grove-agent-plane-fw`:

| direction | rule | source/destination |
|---|---|---|
| inbound | `tcp/22` | `var.admin_ip_cidrs` |
| inbound | `tcp/8384` | `var.admin_ip_cidrs` |
| inbound | `tcp+udp/22000` | **off by default** (`agent_plane_syncthing_sync_public`) |
| outbound | `tcp/1-65535`, `udp/1-65535`, `icmp` | `0.0.0.0/0`, `::/0` |

The droplet is resolved via a `data` source, so **Terraform never owns the
droplet's lifecycle** — `terraform destroy` in this env cannot delete the agent
plane. Attaching a cloud firewall is an in-place change on the firewall object:
no reboot, no replace, no droplet touch.

**Do not "tighten" the outbound rules.** A DO firewall with no outbound rules
blocks *all* egress, and this box is nothing but egress (Anthropic API, GitHub,
DO API, ghcr.io, the Cloudflare tunnel, OTLP to `grove-obs:5080`, apt). Trimming
them takes the company's automation offline in one apply.

---

## Step 0 — (optional, 5 s) confirm the exposure from off-host

From your laptop, *not* from the droplet:

```bash
nc -vz 159.223.171.231 22
nc -vz 159.223.171.231 8384
```

Both should connect **before** the apply. The agent could only measure this as a
host-local hairpin (it runs in a container on this droplet), which proves the
bind address is `0.0.0.0` but not the absence of an upstream filter.

## Step 1 — confirm your own SSH source IP is allowlisted ⚠️ do not skip

```bash
curl -4 -s ifconfig.me; echo
grep -A12 'variable "admin_ip_cidrs"' \
  infra/terraform/environments/production/variables.tf | grep -E '^\s+"'
```

Codified today: `173.84.140.152/32`, `74.47.41.38/32`.

If your current address is **not** in that list, **stop**: append it to
`var.admin_ip_cidrs` in a PR first (GOL-1842 — add, never replace). An
out-of-band DO rule added by hand is silently removed by the next apply.

## Step 2 — plan, scoped to just this firewall

```bash
cd infra/terraform/environments/production
op run --env-file=.env.op -- terraform plan \
  -var 'agent_plane_firewall_enabled=true' \
  -target=digitalocean_firewall.agent_plane
```

**Expected, exactly:**

```
data.digitalocean_droplet.agent_plane[0]: Read complete ... [name=agenticos-droplet]
  # digitalocean_firewall.agent_plane[0] will be created
      + droplet_ids = [ + 572389418 ]
      + name        = "grove-agent-plane-fw"
Plan: 1 to add, 0 to change, 0 to destroy.
```

**Abort if** the plan shows anything other than `1 to add` — in particular any
`digitalocean_droplet` replace, any change to `grove-prod-odoo-fw` /
`grove-prod-blogs-fw`, or a `droplet_ids` that is not exactly `[572389418]`.
(Verified 2026-09-29 from the agent plane against the live DO API: with the flag
at its `false` default the plan is `No changes`; with it `true` it is exactly
`1 to add, 0 to change, 0 to destroy`.)

## Step 3 — apply

```bash
op run --env-file=.env.op -- terraform apply \
  -var 'agent_plane_firewall_enabled=true' \
  -target=digitalocean_firewall.agent_plane
```

Then persist the flag so the next full apply doesn't detach it: set
`agent_plane_firewall_enabled = true` in the env's tfvars, or carry the `-var`
in every apply. **A plain `terraform apply` without the var detaches the
firewall** (`count` → 0 destroys it).

## Step 4 — keep your session open

Leave the SSH session you already have **open** until step 5 passes. DO cloud
firewalls do not kill established connections, so an existing session survives
even a bad rule and is your fastest escape hatch.

## Step 5 — prove it, in this order ⚠️

1. **Your own access still works** — from a *new* terminal:
   ```bash
   ssh <your-user>@159.223.171.231 'echo ok'
   ```
   If this fails, go straight to **Rollback**.

2. **The agents still work** — the plane's outbound is what matters. Confirm a
   heartbeat completes, or from the box:
   ```bash
   curl -s -o /dev/null -w '%{http_code}\n' https://api.github.com
   curl -s -o /dev/null -w '%{http_code}\n' https://api.digitalocean.com/v2/account
   ```
   Both should be `200`/`401`-class, not a hang. A hang means the outbound rules
   did not land — roll back.

3. **The dashboard still loads** — open the Paperclip public URL. Expected to be
   unaffected (outbound tunnel, no inbound listener).

4. **The fence is real** — from off-host, on an address *not* in
   `admin_ip_cidrs` (phone hotspot works):
   ```bash
   nc -vz 159.223.171.231 22     # expect: timeout
   nc -vz 159.223.171.231 8384   # expect: timeout
   ```

5. **Membership is recorded** (once PR #745 is merged):
   ```bash
   python3 infra/terraform/scripts/check-firewall-membership.py production
   ```

Tell GOL-2569 when steps 1–4 pass and the agent will run the read-only proof
(`droplet_ids` census via the DO API + a probe from the agent plane) and close
the issue.

## Step 6 — the two follow-ups this does NOT fix

- **`:8384` should not be bound to `0.0.0.0` at all.** The durable fix is
  Syncthing's own config (`GUI Listen Address` → `127.0.0.1:8384`, then reach it
  over an SSH tunnel or Cloudflare Access). Host config, not Terraform.
- **The token is still account-wide** (GOL-2306 item B1). This firewall narrows
  *who can reach the box*; it does nothing about what the token can do once
  someone is on it.

---

## Rollback

Immediate, no plan needed:

```bash
# Option A — Terraform (destroys the firewall object, detaching it)
op run --env-file=.env.op -- terraform apply \
  -var 'agent_plane_firewall_enabled=false' \
  -target=digitalocean_firewall.agent_plane

# Option B — faster, no creds: DO console -> Networking -> Firewalls ->
# grove-agent-plane-fw -> Droplets -> remove agenticos-droplet.
# Detach takes effect immediately.
```

**If you are locked out entirely:** DO console → the droplet → **Access** →
**Launch Recovery Console**. That path is out-of-band and unaffected by cloud
firewalls. From there, detach the firewall with `doctl` on the box, or fix
`admin_ip_cidrs`.

Host-level rules on the droplet are untouched by anything in this runbook.

## Related

- **GOL-2565** — `grove-prod-odoo-fw` membership drift. **Applied**; verified
  `138.197.44.60:22` and `174.138.119.171:22` now filtered from the agent plane
  (2026-09-29).
- **GOL-2566** — legacy Ghost snowflake, same blind spot
  (`production/legacy-ghost-fw.tf`, `docs/RUNBOOK-legacy-ghost-firewall.md`).
- **GOL-2306** — agent-plane credential exposure (the token half).
- **PR #745** — nightly membership watcher. It asserts *codified* firewalls
  contain their *codified* droplets, so an un-codified droplet is invisible to it
  by construction; GOL-2576 extends it to an account-wide census that is not.
- **`General` firewall** (`c6ca14ae-…`) — `droplet_ids = []` but its rules are
  `:22` ← `0.0.0.0/0` plus tcp/udp `25565` ← `0.0.0.0/0`. Empty today, a loaded
  gun tomorrow: attaching anything to it opens SSH to the world. Recommend
  deleting it.
