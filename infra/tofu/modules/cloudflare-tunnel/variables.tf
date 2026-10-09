variable "account_id" {
  description = "Cloudflare account ID that owns the tunnel and the Access application."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.account_id))
    error_message = "account_id must be a 32-character lowercase hex Cloudflare account ID."
  }
}

variable "zone_id" {
  description = "Cloudflare zone ID that holds the hostname's DNS record."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.zone_id))
    error_message = "zone_id must be a 32-character lowercase hex Cloudflare zone ID."
  }
}

variable "name" {
  description = "Tunnel name. Also names the Access application and policy."
  type        = string

  validation {
    condition     = length(trimspace(var.name)) > 0
    error_message = "name must not be empty."
  }
}

variable "hostname" {
  description = "The one public hostname the tunnel serves. A proxied CNAME to the tunnel is created for it."
  type        = string

  # The same rule as frontend/nginx/render-dashboard-public.sh: one DNS name,
  # with no scheme, port, path, wildcard or list.
  validation {
    condition = (
      length(var.hostname) <= 253 &&
      can(regex("^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$", var.hostname))
    )
    error_message = "hostname must be a single DNS name of at least two labels: no scheme, port, path, wildcard or list."
  }
}

variable "origin_service" {
  description = "Where cloudflared sends matched requests, as seen from the cloudflared container (the compose stack's frontend, for both embry0 tunnels)."
  type        = string

  validation {
    condition     = can(regex("^https?://[^/\\s]+$", var.origin_service))
    error_message = "origin_service must be an http:// or https:// origin with no path."
  }
}

variable "exposure" {
  description = <<-EOT
    What the hostname reaches through the tunnel.
    "webhooks": only the webhook routes (and the Telegram callback when
    expose_telegram_callback is true); every other request gets a 404 from
    the tunnel. "full": every path on the hostname, for an origin that does
    its own hardening, such as the dashboard's public vhost.
  EOT
  type        = string

  validation {
    condition     = contains(["webhooks", "full"], var.exposure)
    error_message = "exposure must be \"webhooks\" or \"full\"."
  }
}

variable "expose_telegram_callback" {
  description = "With exposure = \"webhooks\", also route /api/v1/telegram/callback. Set it only when TELEGRAM_WEBHOOK_URL points at this hostname."
  type        = bool
  default     = false
}

variable "extra_ingress_rules" {
  description = <<-EOT
    Rules inserted before the final 404, in order. `path` is a regular
    expression that cloudflared matches anywhere in the request path, so
    anchor it. With exposure = "webhooks", a rule may only refuse
    (service "http_status:<code>") or repeat an allowed webhook path on
    this hostname; anything else fails the plan.
  EOT
  type = list(object({
    hostname = optional(string)
    path     = optional(string)
    service  = string
  }))
  default = []
}

variable "config_src" {
  description = <<-EOT
    "cloudflare": remotely managed; this module writes the ingress and
    outputs the run token. "local": cloudflared reads its ingress from a
    file on the host (the dashboard tunnel's rendered config.yml), so this
    module manages the tunnel and DNS only.
  EOT
  type        = string
  default     = "cloudflare"

  validation {
    condition     = contains(["cloudflare", "local"], var.config_src)
    error_message = "config_src must be \"cloudflare\" or \"local\"."
  }
}

variable "tunnel_secret" {
  description = "Base64 secret of at least 32 bytes. Needed to create a local tunnel, which then runs from the credentials_json output. Leave null when importing an existing tunnel: Cloudflare never returns it."
  type        = string
  default     = null
  sensitive   = true

  validation {
    condition     = var.tunnel_secret == null || can(base64decode(var.tunnel_secret))
    error_message = "tunnel_secret must be base64."
  }
}

variable "access" {
  description = "Put the hostname behind a Cloudflare Access application that allows these emails or email domains. Null for none. Not allowed with exposure = \"webhooks\": webhook senders cannot log in."
  type = object({
    allowed_emails        = optional(list(string), [])
    allowed_email_domains = optional(list(string), [])
    session_duration      = optional(string, "24h")
  })
  default = null

  validation {
    condition = var.access == null ? true : (
      length(var.access.allowed_emails) + length(var.access.allowed_email_domains) > 0
    )
    error_message = "access must allow at least one email or email domain."
  }
}
