terraform {
  # mock_provider in `tofu test` needs 1.8.
  required_version = ">= 1.8.0"

  required_providers {
    cloudflare = {
      source = "cloudflare/cloudflare"
      # Exact pin: v5 renamed most resources, and its schemas still move
      # between minors. Bump deliberately, then rerun `tofu test`.
      version = "5.27.0"
    }
  }
}
