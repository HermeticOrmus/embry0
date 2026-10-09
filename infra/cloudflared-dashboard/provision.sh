#!/usr/bin/env bash
# Provision the embry0 public-dashboard tunnel (RAV-1069, Pattern B).
#
# Creates a named Cloudflare tunnel (config_src: local) + a proxied CNAME
# <DASHBOARD_PUBLIC_HOSTNAME> -> <tunnel-uuid>.cfargotunnel.com, writes
# creds.json (gitignored, chmod 600) and upserts CF_TUNNEL_ID / CF_RUN_USER
# into infra/.env for docker-compose. Idempotent; --destroy tears everything
# down.
#
# The hostname is DASHBOARD_PUBLIC_HOSTNAME, from the environment or
# infra/.env: the variable the tunnel's ingress and nginx's public vhost are
# rendered from, so the CNAME, the tunnel and the vhost cannot disagree. It is
# checked by the same script and rules (frontend/nginx/render-dashboard-public.sh)
# before anything touches Cloudflare; unset or invalid refuses to run.
#
# Needs CF_API_TOKEN in the environment or infra/.env (a zone-admin token for
# the zone that will host the dashboard). Requires CF_ACCOUNT_ID and
# CF_ZONE_ID; this script has no account or zone default.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_ENV="$HERE/../.env"
RENDER="$HERE/../../frontend/nginx/render-dashboard-public.sh"

# A value from infra/.env, with surrounding quotes removed as compose does.
env_file_value() {
  local v
  v="$(grep -m1 "^$1=" "$INFRA_ENV" 2>/dev/null | cut -d= -f2- || true)"
  v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  printf '%s' "$v"
}

if [[ -z "${DASHBOARD_PUBLIC_HOSTNAME:-}" ]]; then
  DASHBOARD_PUBLIC_HOSTNAME="$(env_file_value DASHBOARD_PUBLIC_HOSTNAME)"
fi
export DASHBOARD_PUBLIC_HOSTNAME
sh "$RENDER" --check || { echo "set DASHBOARD_PUBLIC_HOSTNAME (env or $INFRA_ENV) to the dashboard's hostname" >&2; exit 1; }
HOST="$DASHBOARD_PUBLIC_HOSTNAME"

API="https://api.cloudflare.com/client/v4"
ACCOUNT_ID="${CF_ACCOUNT_ID:?CF_ACCOUNT_ID is required}"
ZONE_ID="${CF_ZONE_ID:?CF_ZONE_ID is required}"
TUNNEL_NAME="embry0-dashboard"
CREDS="$HERE/creds.json"

if [[ -z "${CF_API_TOKEN:-}" && -f "$INFRA_ENV" ]]; then
  CF_API_TOKEN="$(grep -m1 '^CF_API_TOKEN=' "$INFRA_ENV" | cut -d= -f2- || true)"
fi
[[ -n "${CF_API_TOKEN:-}" ]] || { echo "CF_API_TOKEN not set (env or $INFRA_ENV)"; exit 1; }

cf() {
  local method="$1" path="$2"; shift 2
  curl -sS -X "$method" "$API$path" \
    -H "Authorization: Bearer $CF_API_TOKEN" \
    -H "Content-Type: application/json" "$@"
}

json_get() { python3 -c "import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]) or '')" "$1"; }

tunnel_id() {
  cf GET "/accounts/$ACCOUNT_ID/cfd_tunnel?name=$TUNNEL_NAME&is_deleted=false" \
    | json_get "d['result'][0]['id'] if d['result'] else ''"
}

dns_record_id() {
  cf GET "/zones/$ZONE_ID/dns_records?name=$1" \
    | json_get "d['result'][0]['id'] if d['result'] else ''"
}

upsert_env() {
  local key="$1" val="$2"
  touch "$INFRA_ENV"
  if grep -q "^$key=" "$INFRA_ENV"; then
    sed -i "s|^$key=.*|$key=$val|" "$INFRA_ENV"
  else
    printf '%s=%s\n' "$key" "$val" >> "$INFRA_ENV"
  fi
}

if [[ "${1:-}" == "--destroy" ]]; then
  rid="$(dns_record_id "$HOST")"
  [[ -n "$rid" ]] && cf DELETE "/zones/$ZONE_ID/dns_records/$rid" > /dev/null && echo "DNS $HOST deleted"
  tid="$(tunnel_id)"
  [[ -n "$tid" ]] && cf DELETE "/accounts/$ACCOUNT_ID/cfd_tunnel/$tid" > /dev/null && echo "tunnel $TUNNEL_NAME deleted"
  rm -f "$CREDS"
  exit 0
fi

tid="$(tunnel_id)"
if [[ -z "$tid" ]]; then
  secret="$(openssl rand -base64 32)"
  resp="$(cf POST "/accounts/$ACCOUNT_ID/cfd_tunnel" --data "{\"name\":\"$TUNNEL_NAME\",\"tunnel_secret\":\"$secret\",\"config_src\":\"local\"}")"
  tid="$(echo "$resp" | json_get "d['result']['id'] if d.get('result') else ''")"
  [[ -n "$tid" ]] || { echo "tunnel create failed: $resp"; exit 1; }
  printf '{"AccountTag":"%s","TunnelID":"%s","TunnelSecret":"%s"}\n' "$ACCOUNT_ID" "$tid" "$secret" > "$CREDS"
  chmod 600 "$CREDS"
  echo "tunnel created: $tid"
elif [[ ! -f "$CREDS" ]]; then
  echo "tunnel $TUNNEL_NAME exists but $CREDS is missing — the secret is unrecoverable."
  echo "Run '$0 --destroy' and re-provision."
  exit 1
else
  echo "tunnel exists: $tid"
fi

target="$tid.cfargotunnel.com"
rid="$(dns_record_id "$HOST")"
body="{\"type\":\"CNAME\",\"name\":\"$HOST\",\"content\":\"$target\",\"proxied\":true,\"ttl\":1}"
if [[ -n "$rid" ]]; then
  cf PUT "/zones/$ZONE_ID/dns_records/$rid" --data "$body" > /dev/null
  echo "DNS updated: $HOST -> $target"
else
  cf POST "/zones/$ZONE_ID/dns_records" --data "$body" > /dev/null
  echo "DNS created: $HOST -> $target"
fi

upsert_env CF_TUNNEL_ID "$tid"
upsert_env CF_RUN_USER "$(id -u):$(id -g)"
echo "infra/.env updated (CF_TUNNEL_ID, CF_RUN_USER)"
echo "next: docker compose --profile dashboard-tunnel up -d cloudflared-dashboard"
