locals {
  remote = var.config_src == "cloudflare"

  # The routes the webhooks tunnel publishes, from "What each ingress
  # reaches" in docs/ops/deploy.md. /api/v1/webhook/alertmanager is not one:
  # Alertmanager posts to the orchestrator over the LAN.
  webhook_paths = concat(
    ["/api/v1/webhook", "/api/v1/webhook/linear", "/api/v1/webhook/gitlab"],
    var.expose_telegram_callback ? ["/api/v1/telegram/callback"] : [],
  )

  # cloudflared matches `path` as an unanchored regular expression, so a bare
  # "/api/v1/webhook" would also match /api/v1/webhook/alertmanager. Every
  # allowed path is anchored at both ends. None contains a regex metacharacter.
  webhook_path_patterns = [for p in local.webhook_paths : "^${p}$"]

  routed_rules = var.exposure == "webhooks" ? [
    for pattern in local.webhook_path_patterns : {
      hostname = var.hostname
      path     = pattern
      service  = var.origin_service
    }
    ] : [{
      hostname = var.hostname
      path     = null
      service  = var.origin_service
  }]

  catch_all = { hostname = null, path = null, service = "http_status:404" }

  ingress = concat(
    local.routed_rules,
    [for r in var.extra_ingress_rules : { hostname = r.hostname, path = r.path, service = r.service }],
    [local.catch_all],
  )

  # The guard on the webhooks tunnel, evaluated on the ingress actually sent
  # to Cloudflare rather than on any one input: every rule before the last
  # either refuses, or routes one allowed webhook path on this hostname; the
  # last rule is a bare 404.
  ingress_routes_only_webhooks = alltrue([
    for r in slice(local.ingress, 0, length(local.ingress) - 1) :
    startswith(r.service, "http_status:") || (
      r.hostname == var.hostname &&
      r.path != null &&
      contains(local.webhook_path_patterns, coalesce(r.path, "-"))
    )
  ])
  ingress_ends_in_404 = local.ingress[length(local.ingress) - 1] == local.catch_all
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "this" {
  account_id    = var.account_id
  name          = var.name
  config_src    = var.config_src
  tunnel_secret = var.tunnel_secret

  lifecycle {
    precondition {
      condition     = var.exposure != "webhooks" || local.remote
      error_message = "A webhooks tunnel must use config_src = \"cloudflare\": with a local config this module cannot enforce its path restriction."
    }
    precondition {
      condition     = local.remote || length(var.extra_ingress_rules) == 0
      error_message = "extra_ingress_rules has no effect with config_src = \"local\"; put the rules in cloudflared's config file."
    }
    precondition {
      condition     = !(var.exposure == "webhooks" && var.access != null)
      error_message = "access cannot be set on a webhooks tunnel: GitHub, GitLab and Linear cannot log in to Cloudflare Access. Webhooks authenticate with their signing secrets."
    }
  }
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "this" {
  count = local.remote ? 1 : 0

  account_id = var.account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.this.id

  config = {
    ingress = local.ingress
  }

  lifecycle {
    precondition {
      condition     = var.exposure != "webhooks" || (local.ingress_routes_only_webhooks && local.ingress_ends_in_404)
      error_message = "The webhooks tunnel's ingress would route more than the webhook paths (${join(", ", local.webhook_path_patterns)}) on ${var.hostname}. Every other rule must refuse with http_status, and the last must be a bare http_status:404."
    }
  }
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "this" {
  count = local.remote ? 1 : 0

  account_id = var.account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.this.id
}

resource "cloudflare_dns_record" "this" {
  zone_id = var.zone_id
  name    = var.hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.this.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
}

resource "cloudflare_zero_trust_access_policy" "this" {
  count = var.access == null ? 0 : 1

  account_id = var.account_id
  name       = "${var.name} allow"
  decision   = "allow"
  include = concat(
    [for e in var.access.allowed_emails : { email = { email = e } }],
    [for d in var.access.allowed_email_domains : { email_domain = { domain = d } }],
  )
  session_duration = var.access.session_duration
}

resource "cloudflare_zero_trust_access_application" "this" {
  count = var.access == null ? 0 : 1

  account_id       = var.account_id
  name             = var.name
  type             = "self_hosted"
  domain           = var.hostname
  session_duration = var.access.session_duration

  policies = [{
    id         = cloudflare_zero_trust_access_policy.this[0].id
    precedence = 1
  }]
}
