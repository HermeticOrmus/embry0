# The webhooks tunnel publishes only the webhook routes. An unrestricted
# webhooks hostname reaches the dashboard and the whole API without the public
# vhost's hardening (docs/ops/deploy.md, "What each ingress reaches"), so
# every way of widening it must fail the plan.
#
# Mocked provider: nothing here talks to Cloudflare.

mock_provider "cloudflare" {}

variables {
  account_id     = "0123456789abcdef0123456789abcdef"
  zone_id        = "fedcba9876543210fedcba9876543210"
  name           = "embry0-webhooks"
  hostname       = "webhooks.example.com"
  origin_service = "http://frontend:80"
  exposure       = "webhooks"
}

run "routes_exactly_the_webhook_paths" {
  command = apply

  assert {
    # jsonencode: a list and a tuple of equal values never compare equal.
    condition = jsonencode(output.ingress) == jsonencode([
      { hostname = "webhooks.example.com", path = "^/api/v1/webhook$", service = "http://frontend:80" },
      { hostname = "webhooks.example.com", path = "^/api/v1/webhook/linear$", service = "http://frontend:80" },
      { hostname = "webhooks.example.com", path = "^/api/v1/webhook/gitlab$", service = "http://frontend:80" },
      { hostname = null, path = null, service = "http_status:404" },
    ])
    error_message = "The webhooks ingress is not exactly the three anchored webhook paths followed by a 404."
  }

  assert {
    condition = jsonencode([
      for r in cloudflare_zero_trust_tunnel_cloudflared_config.this[0].config.ingress :
      { hostname = r.hostname, path = r.path, service = r.service }
    ]) == jsonencode(output.ingress)
    error_message = "The ingress written to Cloudflare differs from the ingress output."
  }

  assert {
    condition     = cloudflare_zero_trust_tunnel_cloudflared.this.config_src == "cloudflare"
    error_message = "The webhooks tunnel is not remotely managed."
  }

  assert {
    condition = (
      cloudflare_dns_record.this.type == "CNAME" &&
      cloudflare_dns_record.this.proxied == true &&
      cloudflare_dns_record.this.content == "${cloudflare_zero_trust_tunnel_cloudflared.this.id}.cfargotunnel.com"
    )
    error_message = "The DNS record is not a proxied CNAME to the tunnel."
  }

  assert {
    condition     = length(cloudflare_zero_trust_access_application.this) == 0
    error_message = "An Access application was created for the webhooks hostname."
  }
}

run "adds_the_telegram_callback_only_when_asked" {
  command = plan

  variables {
    expose_telegram_callback = true
  }

  assert {
    condition = jsonencode([for r in output.ingress : r.path]) == jsonencode([
      "^/api/v1/webhook$",
      "^/api/v1/webhook/linear$",
      "^/api/v1/webhook/gitlab$",
      "^/api/v1/telegram/callback$",
      null,
    ])
    error_message = "The Telegram callback was not added as the fourth anchored path."
  }
}

run "refuses_a_broader_path" {
  command = plan

  variables {
    extra_ingress_rules = [{ hostname = "webhooks.example.com", path = "^/api/", service = "http://frontend:80" }]
  }

  expect_failures = [cloudflare_zero_trust_tunnel_cloudflared_config.this]
}

run "refuses_an_unanchored_webhook_path" {
  # Unanchored, it would also match /api/v1/webhook/alertmanager.
  command = plan

  variables {
    extra_ingress_rules = [{ hostname = "webhooks.example.com", path = "/api/v1/webhook", service = "http://frontend:80" }]
  }

  expect_failures = [cloudflare_zero_trust_tunnel_cloudflared_config.this]
}

run "refuses_the_alertmanager_route" {
  command = plan

  variables {
    extra_ingress_rules = [{ hostname = "webhooks.example.com", path = "^/api/v1/webhook/alertmanager$", service = "http://frontend:80" }]
  }

  expect_failures = [cloudflare_zero_trust_tunnel_cloudflared_config.this]
}

run "refuses_a_whole_hostname_rule" {
  command = plan

  variables {
    extra_ingress_rules = [{ hostname = "webhooks.example.com", service = "http://frontend:80" }]
  }

  expect_failures = [cloudflare_zero_trust_tunnel_cloudflared_config.this]
}

run "refuses_another_hostname" {
  command = plan

  variables {
    extra_ingress_rules = [{ hostname = "other.example.com", path = "^/api/v1/webhook$", service = "http://frontend:80" }]
  }

  expect_failures = [cloudflare_zero_trust_tunnel_cloudflared_config.this]
}

run "refuses_a_catch_all_to_the_origin" {
  command = plan

  variables {
    extra_ingress_rules = [{ service = "http://frontend:80" }]
  }

  expect_failures = [cloudflare_zero_trust_tunnel_cloudflared_config.this]
}

run "allows_an_extra_refusal" {
  command = plan

  variables {
    extra_ingress_rules = [{ hostname = "webhooks.example.com", path = "^/api/v1/webhook/alertmanager$", service = "http_status:403" }]
  }

  assert {
    condition     = length(output.ingress) == 5
    error_message = "An extra refusing rule was not kept."
  }
}

run "refuses_a_local_config" {
  # A local tunnel takes its ingress from a file this module never sees.
  command = plan

  variables {
    config_src = "local"
  }

  expect_failures = [cloudflare_zero_trust_tunnel_cloudflared.this]
}

run "refuses_access" {
  command = plan

  variables {
    access = { allowed_email_domains = ["example.com"] }
  }

  expect_failures = [cloudflare_zero_trust_tunnel_cloudflared.this]
}
