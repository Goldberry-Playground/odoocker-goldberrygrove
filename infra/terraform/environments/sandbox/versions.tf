terraform {
  required_version = ">= 1.10"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.40"
    }
  }

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
    # DigitalOcean Spaces is S3-compatible. Real values live in backend.hcl
    # (git-ignored). See backend.hcl.example for the template.
    use_lockfile = true
  }
}

provider "digitalocean" {
  # Token read from DIGITALOCEAN_TOKEN env var (set in CI via secrets).
  # Spaces credentials are read from SPACES_ACCESS_KEY_ID and
  # SPACES_SECRET_ACCESS_KEY for the S3 backend.
}
