terraform {
  # mock_provider in `tofu test` needs 1.8.
  required_version = ">= 1.8.0"

  required_providers {
    proxmox = {
      source = "bpg/proxmox"
      # Exact pin, and never below 0.93.1 (CVE-2026-25499). Bump
      # deliberately, then rerun `tofu test`.
      version = "0.116.0"
    }
  }
}
