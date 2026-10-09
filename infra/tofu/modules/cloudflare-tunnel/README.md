# cloudflare-tunnel

One Cloudflare tunnel serving one public hostname, with its proxied DNS record
and, optionally, a Cloudflare Access application in front of it. It covers
both of embry0's tunnels ([docs/ops/deploy.md](../../../../docs/ops/deploy.md#ingress)):

| Tunnel | `exposure` | `config_src` | Ingress comes from |
|---|---|---|---|
| Webhooks (`cloudflared`, profile `webhook-tunnel`) | `"webhooks"` | `"cloudflare"` | this module |
| Dashboard (`cloudflared-dashboard`, profile `dashboard-tunnel`) | `"full"` | `"local"` | `infra/cloudflared-dashboard/config.yml.template`, rendered on the host |

It is the code form of `infra/cloudflared-dashboard/provision.sh` and of the
dashboard clicks in [docs/webhooks.md](../../../../docs/webhooks.md#option-a--cloudflare-tunnel-production).
Those manual paths still work.

Provider: `cloudflare/cloudflare` pinned at exactly `5.27.0`. The module
configures no provider; your root module does, with the token in
`CLOUDFLARE_API_TOKEN`. The token needs Cloudflare Tunnel write, DNS write on
the zone and, with `access`, Access: Apps and Policies write.

## Resources

- `cloudflare_zero_trust_tunnel_cloudflared`: the tunnel.
- `cloudflare_zero_trust_tunnel_cloudflared_config`: its ingress, only with
  `config_src = "cloudflare"`.
- `cloudflare_dns_record`: a proxied CNAME, `hostname` to
  `<tunnel id>.cfargotunnel.com`, the same record `provision.sh` writes.
- `cloudflare_zero_trust_access_policy` and
  `cloudflare_zero_trust_access_application`: only when `access` is set.

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `account_id` | string | required | Cloudflare account ID |
| `zone_id` | string | required | Zone holding `hostname` |
| `name` | string | required | Tunnel name; also names the Access application and policy |
| `hostname` | string | required | One DNS name. The rule of `frontend/nginx/render-dashboard-public.sh`: no scheme, port, path, wildcard or list |
| `origin_service` | string | required | `http(s)://host:port` as cloudflared sees it, `http://frontend:80` for both embry0 tunnels |
| `exposure` | string | required | `"webhooks"` or `"full"`, below |
| `expose_telegram_callback` | bool | `false` | Webhooks only: also route `/api/v1/telegram/callback` |
| `extra_ingress_rules` | list | `[]` | `{hostname, path, service}` rules placed before the final 404 |
| `config_src` | string | `"cloudflare"` | `"cloudflare"` (remotely managed) or `"local"` |
| `tunnel_secret` | string, sensitive | `null` | Base64, at least 32 bytes. Needed to create a local tunnel; leave null when importing |
| `access` | object | `null` | `{allowed_emails, allowed_email_domains, session_duration = "24h"}` |

## Outputs

| Name | Description |
|---|---|
| `tunnel_id` | Tunnel UUID; `CF_TUNNEL_ID` for the dashboard tunnel |
| `tunnel_token` | Sensitive. Run token of a remote tunnel: `CLOUDFLARED_TUNNEL_TOKEN` and `TUNNEL_TOKEN` ([docs/webhooks.md](../../../../docs/webhooks.md), step 2) |
| `credentials_json` | Sensitive. `creds.json` for a local tunnel created with `tunnel_secret` |
| `hostname` | The hostname |
| `ingress` | The rules written to a remote tunnel, for review |
| `access_application_aud` | Access audience tag, or null |

## The webhooks restriction

With `exposure = "webhooks"` the ingress is, in order:

```
webhooks.example.com  ^/api/v1/webhook$            -> origin_service
webhooks.example.com  ^/api/v1/webhook/linear$     -> origin_service
webhooks.example.com  ^/api/v1/webhook/gitlab$     -> origin_service
webhooks.example.com  ^/api/v1/telegram/callback$  -> origin_service   (expose_telegram_callback only)
*                                                  -> http_status:404
```

cloudflared matches `path` as a regular expression anywhere in the request
path, so each path is anchored: a bare `/api/v1/webhook` would also match
`/api/v1/webhook/alertmanager`, which must stay on the LAN.

A precondition on the tunnel config checks the ingress that is actually sent
to Cloudflare, not one input. Every rule before the last must either refuse
(`service = "http_status:<code>"`) or route one of the paths above on
`hostname`, and the last must be a bare 404. Anything that would route more
fails the plan. Two more preconditions refuse a webhooks tunnel with
`config_src = "local"`, whose ingress this module cannot see, and one with
`access`, which GitHub, GitLab and Linear cannot pass; webhooks authenticate
with their signing secrets.

`tests/webhooks.tftest.hcl` covers each broadening: a wider path, an
unanchored webhook path, the Alertmanager route, the whole hostname, another
hostname, a catch-all to the origin, a local config and Access. Each of those
runs fails when its guard is removed.

## Importing an existing tunnel

Use `import` blocks in your root module, then plan:

```hcl
import {
  to = module.webhooks_tunnel.cloudflare_zero_trust_tunnel_cloudflared.this
  id = "<account_id>/<tunnel_id>"
}
import {
  to = module.webhooks_tunnel.cloudflare_zero_trust_tunnel_cloudflared_config.this[0]
  id = "<account_id>/<tunnel_id>"
}
import {
  to = module.webhooks_tunnel.cloudflare_dns_record.this
  id = "<zone_id>/<dns_record_id>"
}
```

An Access application imports as `accounts/<account_id>/<app_id>`, a policy as
`<account_id>/<policy_id>`. Leave `tunnel_secret` null: Cloudflare never
returns it, so a value would show as a change.

A webhooks tunnel configured by hand often has unanchored paths. Its first
plan then shows the ingress changing to the anchored rules above. That diff is
the restriction being applied; read it before applying.

## Tests

```bash
tofu init -backend=false
tofu test
```

The provider is mocked; nothing contacts Cloudflare and no credentials are
read.
