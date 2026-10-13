terraform {
  required_version = ">= 1.10"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.40"
    }
    # Zips the vendored discord-bridge source into a single cloud-init blob
    # (GOL-598). Run `terraform init -upgrade` once to pull it into the lock.
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }

  # S3-compatible (DO Spaces) backend — same bucket as every other Grove env;
  # only `key` differs. Config lives in backend.hcl (git-ignored). See
  # backend.hcl.example. `terraform init -backend-config=backend.hcl`.
  backend "s3" {
    # S3-native state locking (GOL-40): Terraform >= 1.10 writes
    # <key>.tflock via a conditional PUT (If-None-Match: *).
    #
    # GOL-2584 (verified 2026-09-30): this is a NO-OP on DO Spaces. Spaces
    # ACCEPTS a conditional PUT over an existing object (HTTP 200 instead of
    # 412), so every concurrent run "acquires" the same lock and terraform
    # provides NO mutual exclusion -- two QA teardowns both ran to completion
    # on 2026-09-29 because of this. Re-test with
    # `scripts/tf-state-lock-check.sh probe`; while it fails,
    # `scripts/tf-state-lock-check.sh guard <state-key>` is the only thing
    # standing between two overlapping applies and a corrupted state file.
    #
    # Real backend values live in backend.hcl (git-ignored).
    use_lockfile = true
  }
}
