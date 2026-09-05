terraform {
  required_version = ">= 1.10"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.40"
    }
  }

  # Remote state in grove-tf-state, namespaced under `cutover-archives/`.
  # This is a shared, standalone bucket (not tied to any single droplet env),
  # same backend bucket as every other Grove TF env. Real backend values live
  # in backend.hcl (git-ignored); see backend.hcl.example.
  backend "s3" {
    # S3-native state locking (GOL-40): Terraform >= 1.10 writes
    # <key>.tflock via a conditional PUT (If-None-Match: *). DO Spaces
    # enforces it (2nd writer gets HTTP 412). Matches every other env here.
    use_lockfile = true
  }
}
