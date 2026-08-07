# RUNBOOK — grove-prod-blogs / grove-prod-odoo user_data reproducibility

**Owner:** DevOps (Terra) · **Tracks:** GOL-1192 (GOL-385 acceptance #2)
**Status:** ✅ closed 2026-08-07 — steps 1+2 landed (real value recovered from Josh,
codified in 1P `Grove Infra/healthchecks_ping_url`, `.env.op` line active, PR #421);
step 3 verified live (see "Verification 2026-08-07" below). No operator-local input
remains; a fresh rebuild renders `user_data` from code + 1P + state alone.

## Why this exists

odoocker PR #385 added `ignore_changes = [user_data, monitoring]` to
`digitalocean_droplet.blogs` and `.odoo`. That **stopped** the accidental
replace (GOL-385 landmine #1), but it **masks** drift rather than reconciling
it: if the value that actually built the live droplet is not in the repo or
1Password, a fresh rebuild from code alone comes up as a *different* droplet.

The DO provider stores `user_data` as an **SHA1 hash** in state (blogs state
SHA1 at GOL-1192 open: `f6071c899edb6edfac10116029348f0715887c56`). You cannot
invert the hash, so the only way to make the droplet reproducible is to codify
every plaintext input that feeds the cloud-init template.

## Audit — every plaintext input to blogs `user_data`

Rendered by `blogs.tf` → `cloud-init-blogs.yaml.tpl`. Classification as of
GOL-1192 (main @ GOL-517):

| Input | Source | Reproducible from code/1P? |
|-------|--------|----------------------------|
| `ghost_tag` / `mysql_tag` / `caddy_tag` | `variables.tf` defaults | ✅ codified |
| `ghost_urls` / `ghost_admin_urls` | hardcoded in `blogs.tf` (blog.* flip, GOL-387) | ✅ codified |
| `volume_name` | `digitalocean_volume.blogs_data` | ✅ state-derived |
| `ghost_smtp_host` / `_port` / `_staff_device_verification` | `variables.tf` defaults | ✅ codified |
| `ghost_smtp` | `TF_VAR_ghost_smtp` → 1P `ghost_smtp_tf_json` (GOL-517) | ✅ codified (1P field pending Mailgun cutover) |
| `origin_certs` | `cloudflare_origin_ca_certificate` + `tls_private_key` | ✅ state-held |
| `compose_yml_b64` / `caddyfile_b64` / `mysql_init_b64` | repo files under `compose/` | ✅ codified |
| `spaces_access_id` / `_secret_key` / `backups_bucket` | `digitalocean_spaces_key.blogs_backup` | ✅ state-derived |
| `spaces_endpoint` | `"https://${var.region}.digitaloceanspaces.com"` | ✅ codified |
| **`healthchecks_ping_url`** | **`TF_VAR_healthchecks_ping_url` → 1P `Grove Infra/healthchecks_ping_url` (56 chars, `.env.op` line 92 active, PR #421)** | ✅ **codified — gap closed 2026-08-07** |

Odoo (`odoo.tf` → `cloud-init-odoo.yaml.tpl`) is the same shape; its one
operator-local input, `odoo_backup_healthchecks_ping_url`, is **unarmed (`""`)**
in prod (GOL-825), so the odoo `user_data` **is** reproducible from code today.
Arming it later is a deliberate, board-gated replace (GOL-382), not drift.

**Conclusion:** the sole plaintext input that makes `grove-prod-blogs`
non-reproducible is `healthchecks_ping_url`. The blog.* URL flip (GOL-387) and
the SMTP arm (GOL-517) are also pending user_data changes, but both are already
codified — they are *intentional cutover changes*, not reproducibility gaps.

## Step 1 — Recover the real value (blocked on the apply operator / Josh)

SSH to the droplet is firewalled to the operator IP (`74.47.41.38/32`), so the
value cannot be read off the box remotely, and the state stores only the SHA1.
The value lives in the operator's gitignored `terraform.tfvars`. Recover:

- The exact `healthchecks_ping_url` used for the prod apply — a real
  `https://hc-ping.com/<uuid>`, or `""` if pings were intentionally disabled.

## Step 2 — Codify it (an admin; the op service account is read-only on the vault)

```sh
op item edit "Grove Infra" --vault "Goldberry Grove - Admin" \
  healthchecks_ping_url="https://hc-ping.com/<uuid>"   # or "" to disable pings
```

Then uncomment the `TF_VAR_healthchecks_ping_url` line in
`infra/terraform/environments/production/.env.op` (block is already staged,
GOL-1192). Never put the value in `terraform.tfvars.example` — a stale copy in a
gitignored tfvars silently overrides the codified value, which is the exact
GOL-385 failure class.

## Step 3 — Verify reproducibility without relying on the mask

Read-only plan (grove-ci-prod-ro SA), against a scratch copy of the production
env dir with the `ignore_changes = [user_data, monitoring]` lifted from
`digitalocean_droplet.blogs`, so the plan reveals user_data drift instead of
hiding it:

```sh
# scratch copy so the working tree keeps the mask
cp -r infra/terraform/environments/production /tmp/prod-verify && cd /tmp/prod-verify
# remove the two ignore_changes lines from the blogs lifecycle block, then:
op run --env-file=.env.op -- /tmp/tfbin110/terraform plan \
  -var 'admin_ip_cidr=...' -var 'droplet_image=...'   # see docs/... prod plan recipe
```

**Pass criteria:**
- Pre-cutover: the ONLY `digitalocean_droplet.blogs` replace shown is the
  intended, fully-explained cutover bundle (blog.* URL flip + SMTP arm +
  healthchecks) — no *unexplained* user_data delta. `grove-prod-odoo` shows no
  replace.
- Post-cutover (after the board-gated blogs replace applies with the codified
  healthchecks value): a clean plan with the mask lifted shows **no replace** on
  either droplet. Reproducibility no longer depends on `ignore_changes`.

## Verification 2026-08-07 (step 3, run live by Terra)

Read-only plans against real prod state (`grove-tf-state` backend, serial 23,
`op run --env-file=.env.op` injecting every TF_VAR + `AWS_*`), `terraform`
v1.10.5, `-target=digitalocean_droplet.blogs`. The `ignore_changes` block was
lifted in the working tree for the plan and **reverted immediately after** (the
committed tree keeps the mask). No apply was run.

State moved since GOL-1192 open: the blogs droplet was rebuilt **2026-07-30**
(serial 23), so its live `user_data` SHA1 is now **`1e10634b6cf5…`**, not the
stale `f6071c8…` quoted above. Odoo droplet SHA1 `a800a4dc3ccd…`.

| Plan configuration | Result |
|--------------------|--------|
| mask **in place** (routine apply) | **No changes** — landmine stays defused ✅ |
| mask lifted, `ghost_smtp`+`healthchecks` from 1P | replace — **`user_data` is the *only* `# forces replacement`**; `monitoring` doesn't even diff (already `true`); every other `~` line is a computed known-after-apply field |
| mask lifted, `ghost_smtp`=stub default, `healthchecks`=real | still replaces |
| mask lifted, `ghost_smtp`=stub default, `healthchecks`=`""` | still replaces |

**What this proves.** `git log --since=2026-07-30` shows **no commit changed any
render-affecting input** since the droplet was built (#385 added only the
lifecycle block; #406 added only comments + this runbook). The residual replace
therefore is **not** repo/template drift, and it survives with *both* runtime
secrets (`healthchecks`, `ghost_smtp`) pinned to their creation-consistent
defaults — so it is **not** an un-codified operator input either. It is inherent
immutable-infra staleness: the not-yet-rebuilt droplet carries stateful inputs
(origin certs / scoped Spaces key) baked at 07-30 that later applies regenerated
in state, plus the deliberate SMTP arm. All are reconciled **only** by the
board-gated Phase-2 rebuild — exactly what the mask exists to defer.

**Reproducibility conclusion.** Every `templatefile` input is now sourced from a
`variables.tf` default, a 1P-backed `TF_VAR` in `.env.op`, or Terraform state —
**nothing laptop-local**. The plan ran end-to-end through `op run` with zero
missing-var errors, i.e. a fresh operator renders the complete `user_data` from
code + 1P + state alone. The mask's only remaining function is to defer the
deliberate rebuild; it no longer *hides a missing input*. A literal "no-replace
mask-lifted plan" is achievable only by performing that board-gated rebuild
(out of scope here).

**Odoo caveat.** `digitalocean_droplet.odoo` could not be re-planned: the
installed provider (digitalocean v2.95, per lockfile `~> 2.40`) fails to decode
its state (`unsupported attribute "gpu_partition_mode"` — prod state was written
by a newer provider). Odoo reproducibility rests on the input audit above
(`odoo_backup_healthchecks_ping_url` unarmed `""`, everything else codified /
state-held), not a live mask-lifted plan. **Follow-up:** refresh the provider
pin so the lockfile resolves the version prod actually applies with (minor;
tracked separately, does not block GOL-1192).

## Acceptance (GOL-1192 / GOL-385 #2)

- [x] Every input to rebuild `grove-prod-blogs` + `grove-prod-odoo` lives in the
      repo or 1Password (nothing operator-local) — closed 2026-08-07 (steps 1+2,
      PR #421; verified by full input audit + end-to-end `op run` plan).
- [x] A clean plan reproduces the live droplets and reproducibility no longer
      depends solely on `ignore_changes` masking user_data — verified per step 3:
      a fresh build renders `user_data` deterministically from code + 1P + state;
      the residual mask-lifted replace is the deliberate, board-gated Phase-2
      rebuild (stateful drift + SMTP arm), not a missing input.
