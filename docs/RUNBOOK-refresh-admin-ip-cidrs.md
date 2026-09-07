# RUNBOOK — refresh the operator SSH allowlist (`admin_ip_cidrs`)

**Owner:** DevOps (Terra) applies; Engineering (Ada) codifies. **Escalation:** CEO for the apply gate.
**Origin:** GOL-1842 — an ISP-rotated home IP closed port 22 on every Grove droplet for the CEO on launch day.

## When to use this

You (or the CEO) can no longer `ssh`/`scp` to a Grove droplet and the failure is a **connection timeout on port 22** (not `Permission denied`). That means the source address is not in the firewall allowlist — almost always because a home/office ISP rotated the public IP. Symptoms:

- `ssh -v root@<droplet>` hangs at `Connecting to <ip> port 22` then times out.
- Affected hosts share one allowlist, so it fails on **all** of them at once: `grove-prod-odoo`, `grove-prod-blogs`, `grove-obs`, `grove-qa-l3-odoo`, `grove-qa-l3-obs`.

> **Not this runbook:** if you get `Permission denied (publickey)` you *reached* sshd — the allowlist is fine and it's a key problem. See "The key/hostname trap" below.

## The allowlist lives in code

`admin_ip_cidrs` is a `list(string)` consumed by every droplet SSH firewall **and** the managed-Postgres trusted-source rule. It is defined per environment:

- `infra/terraform/environments/production/variables.tf`
- `infra/terraform/environments/qa-app-platform/variables.tf`

Terraform is **authoritative** over these firewalls. A rule added by hand in the DigitalOcean console (or via the DO API) is **silently removed by the next `terraform apply`**. So a hand-added rule is only ever a stopgap; the durable fix is to codify the address here.

## Procedure

### 1. Find your current public IPv4

```
curl -4 ifconfig.me
```

Append `/32`. Example: `173.84.140.152/32`.

### 2. (Optional, urgent) restore access out-of-band

If you're locked out *right now* and can't wait for a PR + apply, add an **additive** inbound TCP/22 rule for your `/32` on the droplet's firewall via the DO console/API. **This is a stopgap only** — record that you did it and still do step 3, or the next apply re-locks you.

### 3. Codify the address (durable fix)

Edit **both** env `variables.tf` files. **Append** to the list default — do **not** replace the existing entries blind (a second operator may depend on one):

```hcl
# before
default = ["74.47.41.38/32", "173.84.140.152/32"]
# after — append the new address
default = ["74.47.41.38/32", "173.84.140.152/32", "<new>/32"]
```

Keep prod and QA in step so the same machine reaches both. Open a PR.

### 4. Confirm it's an in-place update, not a replace

The `Prod plan must not destroy or replace a live resource` check (prod-plan-guard) **must be green** on the PR. Appending to an existing list default converges the firewall **in place**; that green check is the proof. If it's red, the change would destroy/replace a live firewall — stop and investigate before merging.

`Terraform fmt + validate` must also pass on both envs.

### 5. Merge, then apply

Merge the PR. The codified list is not live until `terraform apply` runs against the environment (prod apply is board/CEO-gated — see GOL-1844). **Apply before the next `promote-storefronts.yml` run**, or that pipeline's apply will strip any hand-added stopgap rule from step 2 and re-lock you.

### 6. Prune stale addresses (housekeeping)

Once a rotated address is confirmed dead and no operator uses it, remove it from the list in a follow-up PR (same in-place check applies). Don't prune and add in the same panic — add first, prune later.

## The key/hostname trap

Reaching sshd but getting `Permission denied (publickey)` is **not** an allowlist problem. On the Grove droplets the authorized key is `grove-qa-admin` (see `odoo.tf` `ssh_keys`), and `~/.ssh/config` matches on **hostname, not IP** — so connecting by bare IP offers your default key instead. Force the right key explicitly:

```
ssh -i ~/.ssh/grove-qa-admin root@<droplet-ip>
scp -i ~/.ssh/grove-qa-admin <file> root@<droplet-ip>:/path
```

## Why not a gitignored tfvars / `TF_VAR_admin_ip_cidrs`

The address is kept in the checked-in `variables.tf` default (not a gitignored tfvars) on purpose: it makes the SSH rule reproducible purely from code (GOL-385) so a clean `terraform apply` never drops operator access. The tradeoff is that refreshing an IP is a code PR rather than a config edit — that PR is this runbook.
