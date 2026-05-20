variable "name" {
  description = "Name of the Droplet"
  type        = string
}

variable "size" {
  description = "Droplet slug (e.g. s-4vcpu-8gb)"
  type        = string
}

variable "region" {
  description = "DigitalOcean region slug"
  type        = string
}

variable "image" {
  description = "Droplet OS image slug or ID"
  type        = string
  default     = "ubuntu-24-04-x64"
}

variable "ssh_key_ids" {
  description = "List of SSH key fingerprints or IDs to provision on the Droplet"
  type        = list(string)
}

variable "volume_size_gb" {
  description = "Size (GiB) of the block-storage volume to attach"
  type        = number
}

variable "tags" {
  description = "List of tag names to apply to the Droplet and volume"
  type        = list(string)
  default     = []
}

variable "cloud_init" {
  description = "cloud-config YAML passed as user_data"
  type        = string
  default     = ""
}

variable "monitoring" {
  description = "Enable DigitalOcean agent-based monitoring"
  type        = bool
  default     = false
}
