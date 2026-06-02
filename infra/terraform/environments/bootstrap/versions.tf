terraform {
  required_version = ">= 1.6"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "~> 2.40"
    }
    # AWS provider points at DO Spaces' S3-compatible endpoint, used ONLY for
    # the lifecycle-configuration resource (DO's own provider doesn't expose
    # bucket lifecycle rules as of 2.40).
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.40"
    }
    github = {
      source  = "integrations/github"
      version = "~> 6.2"
    }
  }

  backend "s3" {
    # DigitalOcean Spaces is S3-compatible. Real values live in backend.hcl
    # (git-ignored). See backend.hcl.example for the template.
  }
}
