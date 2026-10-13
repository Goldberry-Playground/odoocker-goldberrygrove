terraform {
  required_version = ">= 1.10"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.40"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Per-PR state isolation — the workflow (Task 3.3) templates `key` as
  # `preview/pr-<number>.tfstate` so multiple PRs can be in flight without
  # state collisions. backend.hcl is git-ignored; see backend.hcl.example.
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

provider "digitalocean" {
  token = var.do_token

  # Spaces creds are needed because the snapshot-restore step inside
  # cloud-init pulls from grove-preview-data (S3-compatible). The
  # provider doesn't *use* them for bucket operations here (we don't
  # create buckets in this env), but we hand them off via templatefile()
  # into the cloud-init script.
  spaces_access_id  = var.spaces_access_key
  spaces_secret_key = var.spaces_secret_key
}
