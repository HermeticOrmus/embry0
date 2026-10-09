variable "cloudflare_account_id" {
  type = string
}

variable "cloudflare_zone_id" {
  type = string
}

variable "dashboard_hostname" {
  type = string
}

variable "dashboard_tunnel_secret" {
  description = "openssl rand -base64 32. Leave null when importing an existing dashboard tunnel."
  type        = string
  default     = null
  sensitive   = true
}

variable "dashboard_access_email_domains" {
  description = "Email domains Cloudflare Access lets through to the dashboard. Empty for no Access application."
  type        = list(string)
  default     = []
}

variable "webhooks_hostname" {
  type = string
}

variable "proxmox_node" {
  type = string
}

variable "vm_id" {
  type = number
}

variable "vm_name" {
  type = string
}
