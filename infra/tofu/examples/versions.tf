terraform {
  required_version = ">= 1.8.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "5.27.0"
    }
    proxmox = {
      source  = "bpg/proxmox"
      version = "0.116.0"
    }
  }

  # Keep state out of this repository: it holds the tunnel secret and run
  # token. Configure a backend in your own root module, for example an S3
  # bucket (MinIO works) with state encryption, or `pg`.
}

# Credentials come from the environment, never from this file:
#   CLOUDFLARE_API_TOKEN                     Cloudflare
#   PROXMOX_VE_ENDPOINT, PROXMOX_VE_API_TOKEN Proxmox
provider "cloudflare" {}

provider "proxmox" {}
