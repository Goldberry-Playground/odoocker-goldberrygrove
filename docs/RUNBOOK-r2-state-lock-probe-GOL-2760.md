# RUNBOOK — Measure whether Cloudflare R2 enforces Terraform state locking (GOL-2760)

**Purpose:** decide ADR-011 option 5 (re-home Terraform state onto Cloudflare R2)
on a **measurement**, not on a compatibility matrix.
**Owner:** DevOps - Terra. **Unblock step 0:** Josh.
**Relates to:** GOL-2760 (this probe), GOL-2755 / [ADR-011](./ADR/011-terraform-state-locking-backend.md) (the decision), GOL-2584 (found the no-op), PR #790 (`tf-state-lock-check.sh`), PR #791 (`tf-state-inventory.sh`, ADR-011).
**Must not run the migration inside the Train #2 window (2026-10-05 … 10-08).** The probe itself is safe any time — it writes one throwaway object to a throwaway bucket.

## Why this cannot be taken on faith

Cloudflare's S3-compatibility matrix lists `If-None-Match` as supported on
`PutObject`. DigitalOcean's docs make a comparable claim, and every Grove
`backend "s3"` block carried the comment *"verified DO Spaces enforces it (2nd
writer gets HTTP 412)"*. Measured 2026-09-30, Spaces answers **200** to the
second conditional PUT, so `use_lockfile = true` is a no-op in all nine envs
including production. Same shape of claim ⇒ same requirement: measure it.

Re-confirmed on the incumbent 2026-09-30T15:18Z (`nyc3.digitaloceanspaces.com` /
`grove-tf-state`): first conditional PUT 200, second **200**, script exit 1.

## Step 0 — BLOCKED ON JOSH: create the R2 credentials

Nothing below can run until these two 1Password fields exist. Measured
2026-09-30: the token at `op://Grove Prod/Cloudflare API Token/credential` is
valid (`/user/tokens/verify` → 200, active) and resolves all four brand zones,
but `GET /accounts/<grove-account>/r2/buckets` → **HTTP 403
`Authentication error`** (account `5579f08c…`, the one that owns all four brand
zones). It is zone-scoped; it cannot list an R2 bucket, let
alone create one. No R2 credential exists in any of the three vaults the agent
plane can read (`Goldberry Grove - Admin`, `Grove Prod`, `Grove QA`).

1. Cloudflare dashboard → **R2** → *Create bucket* → `grove-tf-state-probe`
   (any region; this bucket is throwaway and gets deleted in step 5).
2. **R2** → *Manage R2 API Tokens* → *Create API token* → permission
   **Object Read & Write**, scoped to `grove-tf-state-probe` only.
   R2 returns an **S3-compatible key pair** plus an account endpoint of the form
   `https://<account-id>.r2.cloudflarestorage.com`.
3. Vault the pair on the existing `Grove Infra` item (vault
   `Goldberry Grove - Admin`) as two new fields, exact labels:
   - `r2_access_key_id`
   - `r2_secret_access_key`
4. Reply on GOL-2760 with the **account-id** portion of the endpoint (not a
   secret — it is in every R2 URL). Terra takes it from there.

> Least privilege: an *Object Read & Write* token scoped to one throwaway bucket
> is all the probe needs. Do **not** mint an account-wide
> `Workers R2 Storage: Edit` token for this — if option 5 wins, the migration
> gets its own scoped token on its own ticket.

## Step 1 — AC1: does the second conditional PUT answer 412?

```bash
ACC=<account-id-from-step-0>
export AWS_ACCESS_KEY_ID=$(op read 'op://Goldberry Grove - Admin/Grove Infra/r2_access_key_id')
export AWS_SECRET_ACCESS_KEY=$(op read 'op://Goldberry Grove - Admin/Grove Infra/r2_secret_access_key')

GROVE_S3_HOST="$ACC.r2.cloudflarestorage.com" \
GROVE_S3_REGION=auto \
GROVE_TF_STATE_BUCKET=grove-tf-state-probe \
  bash scripts/tf-state-lock-check.sh probe
```

Read the **exit code**, not the vibe:

| exit | meaning | what to do |
|---:|---|---|
| `0` | second PUT = **412**. R2 enforces `If-None-Match`. | AC1 passes → step 2. |
| `1` | second PUT = 200 (or anything else). | **Option 5 is dead.** Go to "If R2 answers 200". |
| `3` | first PUT was not 200 — nothing was measured. | Fix creds / bucket name / `GROVE_S3_REGION=auto`, then re-run. **Never** report an exit-3 as a FAIL. |

Exit 3 exists because a wrong region or a mis-scoped token makes the first PUT
403, and the pre-GOL-2760 script would have printed `FAIL … not 412` and killed
option 5 on a credential typo.

Paste the full block — it prints endpoint, bucket, region and UTC timestamp, so
the result stays auditable instead of becoming another unverifiable comment.

## Step 2 — AC2: two concurrent plans, one lock

```bash
mkdir -p /tmp/r2locktest && cd /tmp/r2locktest
cat > main.tf <<'TF'
terraform {
  required_version = ">= 1.10"
  backend "s3" {
    bucket                      = "grove-tf-state-probe"
    key                         = "locktest/terraform.tfstate"
    region                      = "auto"
    use_lockfile                = true
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    use_path_style              = true
  }
}
resource "null_resource" "sleep" {
  provisioner "local-exec" { command = "sleep 25" }
}
TF
terraform init -backend-config="endpoints={s3=\"https://$ACC.r2.cloudflarestorage.com\"}"

terraform plan -lock-timeout=0 > /tmp/r2locktest/a.log 2>&1 &
sleep 2
terraform plan -lock-timeout=0 > /tmp/r2locktest/b.log 2>&1
wait
grep -l 'Error acquiring the state lock' /tmp/r2locktest/[ab].log   # expect exactly one
grep -c 'Error releasing the state lock' /tmp/r2locktest/[ab].log   # expect 0 in both
```

**Pass** = exactly one log says `Error acquiring the state lock`, and *neither*
says `Error releasing the state lock … StatusCode: 404`. That 404-on-release is
the 2026-09-29 double-teardown signature: it means both runs "held" the same
lock and the first one deleted it out from under the second.

## Step 3 — AC3: SigV4 + LIST + GET parity

```bash
GROVE_S3_HOST="$ACC.r2.cloudflarestorage.com" \
GROVE_S3_REGION=auto \
GROVE_TF_STATE_BUCKET=grove-tf-state-probe \
  bash scripts/tf-state-inventory.sh
```

Must print the `locktest/terraform.tfstate` object with its managed-resource
count and serial, and report no stale `.tflock`. This proves the read side of
the backend (LIST + GET under R2's SigV4) before anything is migrated — the
inventory script is how a migration gets verified key-by-key later.

## Step 4 — record the verdict on GOL-2755

Paste all three outputs. Then:

- **412 (exit 0) on step 1 and clean steps 2–3** → option 5 is live. Say so on
  GOL-2755 and let the board pick. The migration is a **separate, board-gated**
  ticket and must land outside the Train #2 window.
- **200 (exit 1)** → see below.

## If R2 answers 200

Option 5 is dead — say so plainly on GOL-2755 and **migrate nothing**. Fall back
to ADR-011 **option 1b**: a dedicated state-only PostgreSQL cluster owned by the
`state-backend` env, using Terraform's `backend "pg"`, which takes a real
advisory lock in the database. Do not try to "fix" Spaces or R2 with a wrapper
script; the advisory guard in PR #790 already occupies that ceiling.

## Step 5 — tear down (always, either verdict)

```bash
cd /tmp && rm -rf /tmp/r2locktest
```
Then Josh: delete the `grove-tf-state-probe` bucket **and revoke the scoped R2
API token** in the Cloudflare dashboard. If option 5 wins, the migration mints
its own credential on its own ticket — this one is probe-only and should not
outlive the probe.
