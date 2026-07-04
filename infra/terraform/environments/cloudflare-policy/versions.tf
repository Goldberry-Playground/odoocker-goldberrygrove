terraform {
  required_version = ">= 1.6"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.40"
    }
  }

  # Remote state in grove-tf-state, namespaced under `cloudflare-policy/`.
  # Account-wide edge policy (geo-blocking, future WAF rules) -- deliberately
  # its own env so security policy changes never ride along with app or
  # assets deploys.
  backend "s3" {}
}
