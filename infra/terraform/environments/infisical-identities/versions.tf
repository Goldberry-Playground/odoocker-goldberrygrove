terraform {
  required_version = ">= 1.6"

  required_providers {
    infisical = {
      source  = "Infisical/infisical"
      version = "~> 0.16"
    }
  }

  # Remote state in grove-tf-state, namespaced under `infisical-identities/`.
  # Same backend as bootstrap/preview/production/sandbox — apply order is
  # state-backend first (creates the bucket), then everything else.
  backend "s3" {}
}
