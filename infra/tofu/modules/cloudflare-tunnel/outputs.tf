output "tunnel_id" {
  description = "Tunnel UUID. For a local tunnel, CF_TUNNEL_ID in infra/.env."
  value       = cloudflare_zero_trust_tunnel_cloudflared.this.id
}

output "tunnel_token" {
  description = "Run token of a remotely managed tunnel: CLOUDFLARED_TUNNEL_TOKEN (and TUNNEL_TOKEN) in the env files. Null for a local tunnel."
  value       = local.remote ? data.cloudflare_zero_trust_tunnel_cloudflared_token.this[0].token : null
  sensitive   = true
}

output "credentials_json" {
  description = "creds.json for a local tunnel created with tunnel_secret. Null otherwise."
  value = !local.remote && var.tunnel_secret != null ? jsonencode({
    AccountTag   = var.account_id
    TunnelID     = cloudflare_zero_trust_tunnel_cloudflared.this.id
    TunnelSecret = var.tunnel_secret
  }) : null
  sensitive = true
}

output "hostname" {
  description = "The public hostname."
  value       = var.hostname
}

output "ingress" {
  description = "The ingress rules written to a remotely managed tunnel, in order. Empty for a local tunnel."
  value       = local.remote ? local.ingress : []
}

output "access_application_aud" {
  description = "Audience tag of the Access application, for validating its JWT at the origin. Null without access."
  value       = var.access == null ? null : cloudflare_zero_trust_access_application.this[0].aud
}
