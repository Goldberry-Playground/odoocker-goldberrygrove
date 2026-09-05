# cutover-archives — shared private Spaces bucket for data-cutover archives

Codifies the `grove-cutover-archives` DO Spaces bucket: the private, versioned
home for **pre-migration data-cutover archives** (rollback + audit surface).

This bucket was originally created **imperatively** via the Spaces API to
unblock the WS3a Asana pre-migration archive gate (GOL-2093). This env exists so
it is no longer a snowflake — the bucket, its versioning, its private ACL, and
its lifecycle policy now converge from `terraform apply` (GOL-2101).

## What this manages

| Resource | Purpose |
|---|---|
| `digitalocean_spaces_bucket.cutover_archives` | Bucket named `grove-cutover-archives` (**private** ACL, **versioning enabled**, `prevent_destroy`) + lifecycle rule expiring non-current versions after `var.noncurrent_version_expiration_days` (default 365) and aborting stale multipart uploads after 7 days |

No CDN, no Cloudflare record, no public read, and no service-scoped Spaces key —
intentionally the opposite of the `assets` env. Nothing long-lived consumes this
bucket; operators write to it with the same bootstrap Spaces key that runs the
apply. Add a bucket-scoped `digitalocean_spaces_key` here if/when an automated
writer appears (least privilege).

## Bucket layout

```
grove-cutover-archives/
├── asana/     # full Asana workspace JSON export (GOL-2093, live + verified)
├── square/    # future: Square data cutover archive
└── venmo/     # future: Venmo data cutover archive
```

Each prefix is a point-in-time, pre-migration snapshot kept as the
rollback/audit source-of-truth for that system's migration into Odoo. Prefixes
aren't enforced by the bucket — this just documents the convention.

## What lives here vs. NOT

**Lives here:** pre-migration data-cutover archives (may contain internal task
notes, assignee data, other non-public content — hence private ACL).

**Doesn't:**
- **TF remote state** → `grove-tf-state` bucket
- **Marketing/brand assets** → `grove-assets` bucket (public-read + CDN)
- **Product photos** → Odoo

## Applying

> **The bucket already exists** (created imperatively for GOL-2093). Codifying
> it requires a **one-time `terraform import`** before the first apply, so
> Terraform adopts the live bucket instead of trying to create a duplicate
> (a plain apply would fail with `BucketAlreadyExists`/409).

```bash
# One-time init
cp backend.hcl.example backend.hcl
op run --env-file=.env.op -- terraform init -backend-config=backend.hcl

# One-time import of the pre-existing bucket (region,name)
op run --env-file=.env.op -- terraform import \
  digitalocean_spaces_bucket.cutover_archives nyc3,grove-cutover-archives

# Standard workflow thereafter
op run --env-file=.env.op -- terraform plan
op run --env-file=.env.op -- terraform apply
```

After import, `terraform plan` should show at most the **versioning +
lifecycle** configuration converging (the imperative bucket was created with
versioning + private ACL but no codified lifecycle rule) — never a bucket
create or destroy. Applying prod-adjacent state changes needs board/CEO
approval per repo policy; this env's apply only touches this standalone bucket
(blast radius = the archives themselves, guarded by `prevent_destroy`).

## Cost

- Spaces bucket: **$5/mo** for the first 250 GiB, then $0.02/GiB stored.
- The current Asana archive is a few MB of JSON; non-current versions expire
  after 365 days so re-uploads can't grow storage without bound.

Total: **~$5/mo baseline**, effectively flat for archive-sized workloads.
