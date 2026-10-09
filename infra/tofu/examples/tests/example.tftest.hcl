# The example plans with its placeholder values against mocked providers.
# Run with: tofu test -var-file=terraform.tfvars.example

mock_provider "cloudflare" {}
mock_provider "proxmox" {}

run "plans_with_the_placeholders" {
  command = plan

  assert {
    condition     = module.webhooks_tunnel.ingress[length(module.webhooks_tunnel.ingress) - 1].service == "http_status:404"
    error_message = "The example's webhooks tunnel does not end in a 404."
  }
}
