# RUNBOOK — Harden agent-plane prod-credential exposure (GOL-2306)

> Follow-up to **GOL-2282** (prod SSH firewall provenance). The firewall guarantee
> holds (prod `:22` restricted to Josh's 2 WV IPs; agent droplet `159.223.171.231`
> is *not* on the allow-list). This runbook closes the two **credential bypasses**
> that make that firewall a soft boundary rather than a hard one.
>
> **Status:** remediation gated on **CEO approval + Josh** (prod-side key removal /
> DO token rescope). An agent cannot rotate prod creds or Josh's DO account token
> unilaterally. This doc is the codified plan; execute after approval.

---

## 1. Verified exposure (facts, not the parent-issue summary)

Verified on the shared AgenticOS agent droplet (`hostname cf64223ae91e`, egress
IP `159.223.171.231`) on 2026-09-14:

| # | Finding | Evidence |
|---|---------|----------|
| E1 | **`grove_qa_admin` SSH private key present on the agent host** — `~/.ssh/grove_qa_admin`, ed25519, `SHA256:gU+VD1s01lom0nAD33Je1tkv3LLOejmT1RTzcsFcLYI`, comment `grove-qa-admin@josh-Joshuas-MacBook-Pro`. | `ls -la ~/.ssh`; `ssh-keygen -lf ~/.ssh/grove_qa_admin` (public fingerprint only — private material not read/exfiltrated). |
| E2 | **DO API auth material present on the agent host** — `~/.config/doctl/config.yaml` (23 KB, auth contexts). NB: the `doctl` **binary is not on `PATH`** (not installed / no snap), so the CLI would need installing — but the token in the config is directly usable against `api.digitalocean.com` via `curl`. Token contents **not read**. | `ls -la ~/.config/doctl/`; `command -v doctl` → not found. |
| E3 | **`grove-qa-admin` is one of the keys authorized on prod** (and 3 other surfaces). | `data.digitalocean_ssh_key.qa_admin.fingerprint` in droplet `ssh_keys`. |

### Surfaces that trust `grove-qa-admin` (blast radius of E1)

| Surface | File:line |
|---------|-----------|
| prod Odoo droplet (`grove-prod-odoo`) | `infra/terraform/environments/production/odoo.tf:146` |
| prod Ghost/blogs droplet (`grove-prod-blogs`) | `infra/terraform/environments/production/blogs.tf:121` |
| observability droplet | `infra/terraform/environments/observability/main.tf:105` (var default `grove-qa-admin`, `variables.tf:33`) |
| QA App Platform L3 droplets | `infra/terraform/environments/qa-app-platform/main.tf:110,368` |

`grove-qa-deploy` (the other prod-authorized key) is **not** present on the agent
host — only `grove-qa-admin` is the leaked key.

## 2. The standing bypass (why the firewall isn't enough)

A malicious or compromised agent run on `159.223.171.231` could, entirely with
credentials already on the box:

1. Use the DO token in `~/.config/doctl/config.yaml` (via `curl` to the DO API,
   or by installing `doctl`) to add its own egress IP to the inbound `:22` rule
   of `grove-prod-odoo-fw`.
2. `ssh -i ~/.ssh/grove_qa_admin` into `grove-prod-odoo` (or `-blogs`) — the key
   is authorized there.
3. Revert the firewall rule to erase the window.

This was **not** exercised in the GOL-2282 event (that edit was Josh from his Mac,
per provenance work) — but it is a real, standing capability. The firewall's WV-IP
allow-list is defeated because the agent plane holds both the key *and* the means
to open the door.

## 3. Remediation

### Design goal
**No key that opens prod, and no DO token that can mutate a prod firewall, may
live on the shared agent plane.** Prod admin access moves to a Josh-held key that
never touches an agent host; the agent-plane DO token is scoped so it cannot touch
prod networking.

### Ordering matters
`ssh_keys` on a `digitalocean_droplet` is applied **only at create time**. Changing
it in Terraform does **not** remove the key from a running droplet's
`authorized_keys`; that requires either a droplet **replace** (board-gated for prod,
destroys root-disk state — durable data is on `LABEL=` volumes) or a manual
`authorized_keys` edit on the running box. So the *fastest* way to sever the live
bypass is the manual edit (step A1), independent of the codified TF change (step C).

---

### A. Josh — prod-side (severs the live bypass immediately; no rebuild)

> Run from Josh's Mac (an allow-listed WV IP). These do not require Terraform.

- **A1. Remove `grove-qa-admin` from running prod `authorized_keys`.**
  On `grove-prod-odoo` and `grove-prod-blogs`, delete the line whose fingerprint is
  `SHA256:gU+VD1s01lom0nAD33Je1tkv3LLOejmT1RTzcsFcLYI` from `/root/.ssh/authorized_keys`
  (and any deploy user's `authorized_keys`). Verify:
  ```
  for h in grove-prod-odoo grove-prod-blogs; do
    ssh root@$h "ssh-keygen -lf /root/.ssh/authorized_keys | grep -c gU+VD1s01lom0nAD33Je1tkv3LLOejmT1RTzcsFcLYI"
  done   # expect 0 on both
  ```
- **A2. Mint the prod-only admin key** on Josh's Mac and upload it to DO **under a
  new name** (do **not** reuse `grove-qa-admin`):
  ```
  ssh-keygen -t ed25519 -f ~/.ssh/grove_prod_admin -C "grove-prod-admin@josh"
  doctl compute ssh-key import grove-prod-admin --public-key-file ~/.ssh/grove_prod_admin.pub
  ```
  Add its pubkey to prod `authorized_keys` (so admin access is preserved) — keep the
  private half **only** on Josh's Mac, never on any agent host.
- **A3. Rotate `grove-qa-admin`** (optional but recommended): generate fresh material
  for QA/obs use and replace the DO key contents; whatever material remains on the
  agent host must no longer be trusted by prod (guaranteed by A1 + C).

### B. Josh — agent-plane DO token (removes the firewall-mutation capability)

- **B1. Replace the token in the agent host `~/.config/doctl/config.yaml`** with a
  DO token scoped so it **cannot** write networking / firewalls (least privilege).
  DO PATs are account-wide read/write today, so the robust options are:
  - a **read-only** PAT for the agent plane (agents can `list`/`get` but not mutate
    prod firewall), and/or
  - move any legitimate agent-plane DO writes behind a reviewed CI job that holds a
    narrowly-scoped token in 1Password, not a standing token on the shared host.
- **B2.** (defense-in-depth) Prefer **per-run secret injection** for agent-held QA
  keys over a persistent `~/.ssh/grove_qa_admin` file — inject at job start, shred
  at job end — so no long-lived prod-capable key sits at rest on the shared host.

### C. Terra (me) — codified so the next prod rebuild never re-trusts the leaked key

**Gated on:** CEO approval **and** A2 done (the `grove-prod-admin` DO key must exist,
or `terraform plan` errors on the data source). Ship as a PR carrying SHA-bound
human approval (prod protected-path guard).

Diff (both prod droplets):

```hcl
# infra/terraform/environments/production/odoo.tf  AND  blogs.tf
 data "digitalocean_ssh_key" "qa_deploy" {
   name = "grove-qa-deploy"
 }

-data "digitalocean_ssh_key" "qa_admin" {
-  name = "grove-qa-admin"
-}
+# Prod break-glass admin key — Josh-held, never lands on an agent host (GOL-2306).
+data "digitalocean_ssh_key" "prod_admin" {
+  name = "grove-prod-admin"
+}

 resource "digitalocean_droplet" "odoo" {   # (blogs.blogs likewise)
   ...
   ssh_keys = [
     data.digitalocean_ssh_key.qa_deploy.fingerprint,
-    data.digitalocean_ssh_key.qa_admin.fingerprint,
+    data.digitalocean_ssh_key.prod_admin.fingerprint,
   ]
```

Notes:
- `qa_deploy` stays (deploy path; its private half is **not** on the agent host).
- **Inert until rebuild:** prod droplet `user_data` is in `ignore_changes` and
  `ssh_keys` only applies at create — so this PR does **not** touch running prod;
  it guarantees the *next* board-gated rebuild trusts only `qa_deploy + prod_admin`.
  The live severing is done by A1, not by this diff.
- **observability** (`main.tf:105`) also defaults to `grove-qa-admin`. Lower
  sensitivity (not a money/prod-Odoo surface) — track its swap to `grove-prod-admin`
  (or an obs-scoped key) as a NON-BLOCKING follow-up, same pattern.
- **QA** (`qa-app-platform`) legitimately keeps `grove-qa-admin` — that's its home
  scope. The whole point is that the QA key no longer opens *prod*.

## 4. Verification / done bar

- A1: fingerprint grep returns 0 on both prod boxes (command above).
- B1: agent-host DO token cannot mutate `grove-prod-odoo-fw` (attempt a scoped
  `list` succeeds, a firewall `update` is denied).
- C: `terraform plan` in `environments/production` is clean and shows **no** change
  to running droplets (data-source rename + ssh_keys list swap only; no
  replace, because `ssh_keys` change is create-time and the resource isn't being
  recreated by this diff).

## 5. Residual after remediation

- `grove-qa-admin` still opens QA + obs (by design). If QA/obs are considered
  sensitive later, extend the `prod_admin`-style split there too.
- Long-lived DO PAT on the agent host is replaced but the model is still
  "standing token on shared host" unless B2 (per-run injection) is adopted.
