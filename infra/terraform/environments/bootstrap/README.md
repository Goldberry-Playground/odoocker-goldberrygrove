# Grove Preview — Bootstrap

One-time Terraform module that replaces the manual pre-flight steps **P1–P8** from the `grove-preview-environments` implementation plan.

> **Apply order**: this env stores its state in the `grove-tf-state` Spaces bucket. That bucket (and its access keys) are themselves Terraform-managed by [`../state-backend/`](../state-backend/README.md), which must be applied **first**, on a fresh setup. See `state-backend/README.md` for the one-time bootstrap there.

## What this manages

| Step | What | Resource |
|---|---|---|
| P2 | Spaces bucket for sanitized snapshots | `digitalocean_spaces_bucket.preview_data` |
| P2 | Scoped RW Spaces access key | `digitalocean_spaces_key.preview_data_rw` |
| P3 | 7-day lifecycle expiry on `snapshots/` + `filestore/` | `aws_s3_bucket_lifecycle_configuration.preview_data` |
| P4 | DNS delegation from Cloudflare to DigitalOcean | `cloudflare_record.preview_ns` × 3 + `digitalocean_domain.preview` |
| P5 | SSH public key upload to DO | `digitalocean_ssh_key.preview_deploy` |
| P6 | Seven GH Actions secrets on `grove-sites` | `github_actions_secret.preview` × 7 |

## What stays manual

These are the irreducible trust roots — credentials this module itself depends on. **You do them once and never again** (unless you rotate them).

1. **DigitalOcean API token** — https://cloud.digitalocean.com/account/api/tokens. Scopes: `droplet`, `domain`, `firewall`, `spaces`, `tag` (read+write); `account` (read). Name `grove-preview-terraform`.
2. **GitHub Personal Access Token** — https://github.com/settings/tokens. Either: classic with `repo` scope, OR fine-grained on `Goldberry-Playground/grove-sites` with **Repository permissions → Actions: Read & write** and **Secrets: Read & write**.
3. **Cloudflare API token** — https://dash.cloudflare.com/profile/api-tokens. Use the **Edit zone DNS** template, restricted to `gatheringatthegrove.com`.
4. **Slack incoming-webhook URL** — https://api.slack.com/apps → create app "Grove Ops" → Incoming Webhooks → toggle on → Add to `#ops` channel.
5. **SSH keypair** — generated locally with `ssh-keygen` (the private key never enters tfstate):
   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/grove-preview-deploy -C "grove-preview-deploy" -N ""
   ```

You also need an existing `grove-tf-state` Spaces bucket and RW access key for it — that's the backend this state lives in. **The canonical way to satisfy this is to apply [`../state-backend/`](../state-backend/README.md) first** — it provisions the bucket, creates a bucket-scoped RW Spaces key, and pushes the key as `SPACES_ACCESS_KEY_ID` + `SPACES_SECRET_ACCESS_KEY` GitHub Secrets on odoocker. For local apply of this bootstrap module, fetch the same values into your shell via `op read` from the `GoldberryGrove Infra` 1Password item that state-backend documents.

## First apply

```bash
cd infra/terraform/environments/bootstrap

# One-time: copy the backend template
cp backend.hcl.example backend.hcl   # backend.hcl is git-ignored

# Set credentials in your shell (do not commit). The TF_VAR_* convention
# keeps them out of any tfvars file or shell history if you `export`
# rather than inline-prefix.
export TF_VAR_do_token="dop_v1_..."
export TF_VAR_cloudflare_api_token="..."
export TF_VAR_github_token="ghp_..."
export TF_VAR_slack_ops_webhook="https://hooks.slack.com/services/..."

# Backend credentials for grove-tf-state (existing — same as sandbox uses)
export AWS_ACCESS_KEY_ID="<grove-tf-state RW key>"
export AWS_SECRET_ACCESS_KEY="<grove-tf-state RW secret>"

# Operator inputs (non-sensitive) — provide via tfvars OR more TF_VAR_*
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars   # set admin_ip_cidr to your $(curl -s ifconfig.me)/32

# Standard TF dance
terraform init -backend-config=backend.hcl
terraform plan -out=tfplan
terraform apply tfplan
```

Expected: `Apply complete! Resources: ~15 added, 0 changed, 0 destroyed.`

## Verify

```bash
# DNS delegation (allow 2–5 min for propagation; can take up to 24h)
dig +short NS preview.gatheringatthegrove.com
# Expect three lines: ns1/ns2/ns3.digitalocean.com

# All 7 secrets present on grove-sites
gh secret list --repo Goldberry-Playground/grove-sites | sort

# Bucket exists and is private
s3cmd info s3://grove-preview-data | grep -E "ACL|Lifecycle"

# Slack webhook still works (sanity check from your shell)
curl -X POST -H 'Content-type: application/json' \
  --data '{"text":":wave: bootstrap apply succeeded"}' \
  "$TF_VAR_slack_ops_webhook"
```

## Important caveats

### Destroy-protection is on

`digitalocean_spaces_bucket.preview_data` and `digitalocean_domain.preview` have `lifecycle { prevent_destroy = true }`. A `terraform destroy` will fail loudly on these. To genuinely tear down (which you almost never want to do because you'd lose seven days of sanitized snapshots), edit `main.tf` to remove `prevent_destroy = true` first, then apply, then destroy.

### Secrets in tfstate

The Spaces access/secret key, the SSH private key file contents, the DO token, the Slack webhook, and the GitHub PAT all materialize in `terraform.tfstate`. The state file lives in `s3://grove-tf-state/preview/bootstrap/terraform.tfstate`. Keep the `grove-tf-state` Spaces RW key tightly held (ideally only you have it). DO Spaces server-side-encrypts at rest by default but does not natively support TF state encryption.

### Rotating credentials

To rotate the DO token, GH PAT, Cloudflare token, or Slack webhook:
1. Create the new credential in the upstream service
2. `unset TF_VAR_<old>` and `export TF_VAR_<name>="<new value>"`
3. `terraform apply` — this updates the corresponding GH secret in place
4. Revoke the old credential in the upstream service

### Importing existing resources

If you've already created any of P2/P4/P5 by hand and need to bring them under TF management instead of starting over:

```bash
terraform import digitalocean_spaces_bucket.preview_data nyc3,grove-preview-data
terraform import digitalocean_ssh_key.preview_deploy <numeric-id-from-doctl>
terraform import digitalocean_domain.preview preview.gatheringatthegrove.com
terraform import cloudflare_record.preview_ns[\"ns1.digitalocean.com\"] <zone-id>/<record-id>
# ... etc per resource
```

For GH secrets, you can't truly "import" plaintext values — TF will overwrite whatever's there on the first apply.

## What's NOT here

This module covers preview-environment credentials and DNS. It does **not** cover:

- **Prod CD credentials** (`PROD_SSH_HOST`, `PROD_SSH_PRIVATE_KEY`, etc.) — will be added as a follow-up in the same `local.gh_secrets` map when the M4 (prod CD) milestone lands.
- **Per-PR preview Terraform state buckets** — those are M2's job; this module just verifies the `preview/` prefix is writable.
- **The actual preview-droplet provisioning** — that's M2.
