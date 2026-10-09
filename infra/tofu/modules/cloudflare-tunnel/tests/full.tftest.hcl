# exposure = "full": the whole hostname goes to the origin, the dashboard
# tunnel's shape. Mocked provider: nothing here talks to Cloudflare.

mock_provider "cloudflare" {}

variables {
  account_id     = "0123456789abcdef0123456789abcdef"
  zone_id        = "fedcba9876543210fedcba9876543210"
  name           = "embry0-dashboard"
  hostname       = "dashboard.example.com"
  origin_service = "http://frontend:80"
  exposure       = "full"
}

run "routes_the_hostname_then_404" {
  command = apply

  assert {
    # jsonencode: a list and a tuple of equal values never compare equal.
    condition = jsonencode(output.ingress) == jsonencode([
      { hostname = "dashboard.example.com", path = null, service = "http://frontend:80" },
      { hostname = null, path = null, service = "http_status:404" },
    ])
    error_message = "The full-exposure ingress is not the hostname followed by a 404."
  }

  assert {
    condition     = output.tunnel_token != null
    error_message = "A remotely managed tunnel has no run token."
  }
}

run "keeps_extra_rules_before_the_404" {
  command = plan

  variables {
    extra_ingress_rules = [{ hostname = "dashboard.example.com", path = "^/metrics$", service = "http_status:404" }]
  }

  assert {
    condition     = output.ingress[1].path == "^/metrics$" && output.ingress[2].service == "http_status:404" && output.ingress[2].hostname == null
    error_message = "The extra rule was not placed between the hostname rule and the final 404."
  }
}

run "local_tunnel_manages_no_ingress" {
  command = apply

  variables {
    config_src    = "local"
    tunnel_secret = "AQIDBAUGBwgBAgMEBQYHCAECAwQFBgcIAQIDBAUGBwg="
  }

  assert {
    condition     = length(cloudflare_zero_trust_tunnel_cloudflared_config.this) == 0 && length(output.ingress) == 0
    error_message = "A local tunnel had its ingress managed remotely."
  }

  assert {
    condition     = jsondecode(output.credentials_json).TunnelID == cloudflare_zero_trust_tunnel_cloudflared.this.id
    error_message = "credentials_json does not name the tunnel."
  }

  assert {
    condition     = output.tunnel_token == null
    error_message = "A local tunnel returned a run token."
  }
}

run "local_tunnel_refuses_extra_rules" {
  command = plan

  variables {
    config_src          = "local"
    extra_ingress_rules = [{ hostname = "dashboard.example.com", service = "http://frontend:80" }]
  }

  expect_failures = [cloudflare_zero_trust_tunnel_cloudflared.this]
}

run "access_allows_the_listed_identities" {
  command = apply

  variables {
    access = {
      allowed_emails        = ["operator@example.com"]
      allowed_email_domains = ["example.com"]
    }
  }

  assert {
    condition = (
      cloudflare_zero_trust_access_application.this[0].domain == "dashboard.example.com" &&
      cloudflare_zero_trust_access_application.this[0].type == "self_hosted" &&
      cloudflare_zero_trust_access_application.this[0].policies[0].id == cloudflare_zero_trust_access_policy.this[0].id
    )
    error_message = "The Access application does not protect the hostname with the module's policy."
  }

  assert {
    condition     = cloudflare_zero_trust_access_policy.this[0].decision == "allow" && length(cloudflare_zero_trust_access_policy.this[0].include) == 2
    error_message = "The Access policy does not allow exactly the listed email and domain."
  }
}

run "access_refuses_an_empty_allow_list" {
  command = plan

  variables {
    access = {}
  }

  expect_failures = [var.access]
}

run "refuses_a_url_as_hostname" {
  command = plan

  variables {
    hostname = "https://dashboard.example.com"
  }

  expect_failures = [var.hostname]
}

run "refuses_a_wildcard_hostname" {
  command = plan

  variables {
    hostname = "*.example.com"
  }

  expect_failures = [var.hostname]
}

run "refuses_a_single_label_hostname" {
  command = plan

  variables {
    hostname = "localhost"
  }

  expect_failures = [var.hostname]
}
