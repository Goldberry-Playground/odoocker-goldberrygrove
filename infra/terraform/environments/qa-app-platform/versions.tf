terraform {
  required_version = ">= 1.6"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.40"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.40"
    }
  }

  # Remote state in grove-tf-state, namespaced under `qa-app-platform/`.
  # Distinct path from `qa/` so the two envs share no state during the
  # parallel-cutover validation window (ADR-007 D4).
  backend "s3" {}
}
