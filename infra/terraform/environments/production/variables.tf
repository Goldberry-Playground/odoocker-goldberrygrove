variable "region" {
  description = "DigitalOcean region slug for production Droplets"
  type        = string
  default     = "nyc3"
}

variable "ssh_key_ids" {
  description = "List of DigitalOcean SSH key fingerprints or numeric IDs to provision on all Droplets"
  type        = list(string)
}

variable "admin_cidr" {
  description = "CIDR block allowed to reach SSH (port 22). Restrict to your office/VPN range."
  type        = string
  default     = "0.0.0.0/0" # Override in terraform.tfvars with your real admin IP range
}

variable "domain_zone" {
  description = "Root domain managed in DigitalOcean DNS (must already exist as a domain in your DO account)"
  type        = string
  default     = "gatheringatthegrove.com"
}

# Additional domains are managed via separate digitalocean_domain resources
# (goldberrygrove.farm, woodworkingeorge.com, atthegrovenursery.com).
# See main.tf for the full list.
