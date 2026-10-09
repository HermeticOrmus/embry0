# Usage example only: copy it into a root module of your own, outside this
# repository, with your own backend, values and state.

# The dashboard tunnel. Locally managed, as infra/cloudflared-dashboard runs
# it: cloudflared reads the ingress rendered from DASHBOARD_PUBLIC_HOSTNAME,
# so this manages the tunnel, its DNS and an optional Access application.
module "dashboard_tunnel" {
  source = "../modules/cloudflare-tunnel"

  account_id     = var.cloudflare_account_id
  zone_id        = var.cloudflare_zone_id
  name           = "embry0-dashboard"
  hostname       = var.dashboard_hostname
  origin_service = "http://frontend:80"
  exposure       = "full"
  config_src     = "local"
  tunnel_secret  = var.dashboard_tunnel_secret

  access = length(var.dashboard_access_email_domains) == 0 ? null : {
    allowed_email_domains = var.dashboard_access_email_domains
  }
}

# The webhooks tunnel. Remotely managed, restricted to the webhook routes.
module "webhooks_tunnel" {
  source = "../modules/cloudflare-tunnel"

  account_id     = var.cloudflare_account_id
  zone_id        = var.cloudflare_zone_id
  name           = "embry0-webhooks"
  hostname       = var.webhooks_hostname
  origin_service = "http://frontend:80"
  exposure       = "webhooks"
}

# The host, sized as in docs/ops/provisioning.md §2.
module "host" {
  source = "../modules/proxmox-vm"

  node_name          = var.proxmox_node
  vm_id              = var.vm_id
  name               = var.vm_name
  cores              = 8
  memory_mb          = 16384
  balloon_min_mb     = 4096
  disk_size_gb       = 150
  datastore_id       = "local-lvm"
  disk_image_file_id = "local:import/ubuntu-26.04-server-cloudimg-amd64.qcow2"
  user_data_file_id  = "local:snippets/embry0-user.yaml"
  network_bridge     = "vmbr0"
}

output "dashboard_tunnel_id" {
  description = "CF_TUNNEL_ID in infra/.env."
  value       = module.dashboard_tunnel.tunnel_id
}

output "dashboard_credentials_json" {
  description = "infra/cloudflared-dashboard/creds.json, mode 0600."
  value       = module.dashboard_tunnel.credentials_json
  sensitive   = true
}

output "webhooks_tunnel_token" {
  description = "CLOUDFLARED_TUNNEL_TOKEN in infra/.env and the root .env, and TUNNEL_TOKEN in the root .env."
  value       = module.webhooks_tunnel.tunnel_token
  sensitive   = true
}

output "host_ipv4_addresses" {
  value = module.host.ipv4_addresses
}
