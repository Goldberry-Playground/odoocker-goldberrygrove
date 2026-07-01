terraform {
  required_version = ">= 1.5"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.40"
    }
  }

  # S3-compatible (DO Spaces) backend — same bucket as every other Grove env;
  # only `key` differs. Config lives in backend.hcl (git-ignored). See
  # backend.hcl.example. `terraform init -backend-config=backend.hcl`.
  backend "s3" {}
}
