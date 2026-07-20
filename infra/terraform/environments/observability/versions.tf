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
    # <key>.tflock via a conditional PUT (If-None-Match: *). Verified DO
    # Spaces enforces it (2nd writer gets HTTP 412). Real backend values
    # live in backend.hcl (git-ignored).
    use_lockfile = true
  }
}
